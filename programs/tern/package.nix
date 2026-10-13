{
  lib,
  stdenv,
  stdenvNoCC,
  requireFile,
  glibc,
  patchelf,
  wayland,
  libxkbcommon,
  libGL,
  vulkan-loader,
  libxcb,
  pipewire,
  libpulseaudio,
}:

let
  source = builtins.fromJSON (builtins.readFile ./source.json);
  inherit (source) version;
  # Tern dlopens its graphics, input and audio stacks by soname; a headless host
  # only needs them to resolve, not to run. RPATH is the only place they can
  # come from: the upstream binary links nothing beyond libstdc++ and glibc.
  nativeLibraries = [
    glibc
    stdenv.cc.cc.lib
    wayland
    libxkbcommon
    libGL
    vulkan-loader
    libxcb
    pipewire
    libpulseaudio
  ];
  libraryPath = lib.makeLibraryPath nativeLibraries;
  # Builds sit behind a Stencil sign-in, so the tarball cannot be fetched.
  # homelab-tern update authenticates, adds it to the store and pins this hash.
  tarball = requireFile {
    name = "Tern-${version}-linux-x86_64.tar.gz";
    hash = source.hash;
    message = ''
      Tern ${version} is not in the Nix store. Sign in to Stencil once, then
      fetch and verify the release using the saved refresh token:

        homelab-tern login
        homelab-tern update
    '';
  };
in
stdenvNoCC.mkDerivation {
  pname = "tern";
  inherit version;

  src = tarball;
  sourceRoot = "tern";

  nativeBuildInputs = [ patchelf ];
  # The binary carries an appended payload after its ELF image; the default
  # strip and RPATH-shrinking hooks must not rewrite it.
  dontStrip = true;
  dontPatchELF = true;

  installPhase = ''
    runHook preInstall

    install -Dm755 tern "$out/bin/tern"
    patchelf \
      --set-interpreter ${glibc}/lib/ld-linux-x86-64.so.2 \
      --force-rpath --set-rpath "${libraryPath}" \
      "$out/bin/tern"

    # The tarball cannot be re-fetched, and every glibc bump rebuilds this
    # package from it. Referencing it keeps it in the system closure, so garbage
    # collection cannot delete the one copy.
    install -Dm644 /dev/null "$out/nix-support/upstream-tarball"
    echo ${tarball} > "$out/nix-support/upstream-tarball"

    runHook postInstall
  '';

  doInstallCheck = true;
  installCheckPhase = ''
    runHook preInstallCheck

    grep -qF "tern ${version} " <<< "$("$out/bin/tern" --version)"

    # Every library Tern dlopens on this platform must resolve through RPATH.
    IFS=: read -ra directories <<< "$(patchelf --print-rpath "$out/bin/tern")"
    for library in libwayland-client.so.0 libxkbcommon.so.0 libEGL.so.1 libGLESv2.so.2 \
        libvulkan.so.1 libxcb.so.1 libpipewire-0.3.so.0 libpulse-simple.so.0; do
      found=
      for directory in "''${directories[@]}"; do
        [[ -e "$directory/$library" ]] && found=1
      done
      [[ -n $found ]] || { echo "$library is not on Tern's RPATH" >&2; exit 1; }
    done

    # The embedded web client must be served by the binary itself.
    export TERN_CONFIG_DIR="$TMPDIR/tern-config"
    "$out/bin/tern" web serve --listen 127.0.0.1:18377 >/dev/null 2>&1 &
    server=$!
    trap 'kill "$server" 2>/dev/null || true' EXIT
    for _ in $(seq 50); do
      exec 3<>/dev/tcp/127.0.0.1/18377 2>/dev/null && break
      sleep 0.1
    done
    printf 'GET / HTTP/1.0\r\n\r\n' >&3
    head -n 1 <&3 | grep -qF "200"

    runHook postInstallCheck
  '';

  meta = {
    description = "Tern multiplexing terminal, packaged from the upstream Linux x86_64 release";
    homepage = "https://stencil.so";
    license = lib.licenses.unfree;
    platforms = [ "x86_64-linux" ];
    mainProgram = "tern";
    sourceProvenance = [ lib.sourceTypes.binaryNativeCode ];
  };
}
