{
  lib,
  stdenv,
  stdenvNoCC,
  fetchurl,
  glibc,
  alsa-lib,
  libpulseaudio,
  patchelf,
  python3,
  makeWrapper,
}:

let
  source = builtins.fromJSON (builtins.readFile ./omp-source.json);
  inherit (source) version;
  nativeLibraries = [
    glibc
    stdenv.cc.cc.lib
    alsa-lib
    libpulseaudio
  ];
  libraryPath = lib.makeLibraryPath nativeLibraries;
in
stdenvNoCC.mkDerivation {
  pname = "omp";
  inherit version;

  src = fetchurl {
    url = "https://github.com/can1357/oh-my-pi/releases/download/v${version}/omp-linux-x64";
    hash = source.hash;
  };

  nativeBuildInputs = [
    patchelf
    python3
    makeWrapper
  ];
  dontUnpack = true;
  dontStrip = true;
  # The embedded addons need these paths even though their DT_NEEDED entries
  # are invisible to Nix's ordinary RPATH shrinking hook.
  dontPatchELF = true;

  installPhase = ''
    runHook preInstall

    install -Dm755 "$src" "$out/libexec/omp"
    # RPATH also reaches the addons embedded in the upstream executable.
    # Preload libstdc++ as upstream's Nix package does for dlopen'd addons.
    patchelf \
      --set-interpreter ${glibc}/lib/ld-linux-x86-64.so.2 \
      --force-rpath --set-rpath "${libraryPath}" \
      --add-needed libstdc++.so.6 \
      "$out/libexec/omp"
    python3 ${./fix-elf-verdef.py} "$out/libexec/omp"

    makeWrapper "$out/libexec/omp" "$out/bin/omp" \
      --set-default OMP_NATIVE_LIBRARY_PATH "${libraryPath}"

    runHook postInstall
  '';

  doInstallCheck = true;
  installCheckPhase = ''
    runHook preInstallCheck

    # PI_CONFIG_DIR is resolved relative to the builder's home directory.
    # Redirect both global daemon state and agent state into the build sandbox.
    export PI_CONFIG_DIR="$(realpath -m --relative-to="$HOME" "$TMPDIR/omp-config")"
    export PI_CODING_AGENT_DIR="$TMPDIR/omp-config/agent"
    # The native-addon cache independently honors XDG_DATA_HOME.
    export XDG_DATA_HOME="$TMPDIR/omp-data"
    mkdir -p "$PI_CODING_AGENT_DIR" "$XDG_DATA_HOME/omp"
    versionOutput="$("$out/bin/omp" --version)"
    test "$versionOutput" = "omp/${version}"
    smokeOutput="$("$out/bin/omp" --smoke-test)"
    grep -F "smoke-test: ok" <<< "$smokeOutput"

    # Prove native libraries resolve without an ambient LD_LIBRARY_PATH.
    env -u LD_LIBRARY_PATH BUN_BE_BUN=1 "$out/bin/omp" -e '
      const { dlopen } = require("bun:ffi");
      const libraries = {
        "libstdc++.so.6": { __cxa_demangle: { args: ["ptr", "ptr", "ptr", "ptr"], returns: "ptr" } },
        "libgcc_s.so.1": { _Unwind_Backtrace: { args: ["ptr", "ptr"], returns: "i32" } },
        "libasound.so.2": { snd_asoundlib_version: { args: [], returns: "cstring" } },
        "libpulse.so.0": { pa_get_library_version: { args: [], returns: "cstring" } },
        "libpulse-simple.so.0": { pa_simple_free: { args: ["ptr"], returns: "void" } },
      };
      for (const [name, symbols] of Object.entries(libraries)) {
        dlopen(name, symbols).close();
      }
    '

    runHook postInstallCheck
  '';

  meta = {
    description = "OMP coding agent, packaged from the upstream Linux x64 release";
    homepage = "https://github.com/can1357/oh-my-pi";
    license = lib.licenses.mit;
    platforms = [ "x86_64-linux" ];
    mainProgram = "omp";
    sourceProvenance = [ lib.sourceTypes.binaryNativeCode ];
  };
}
