{
  lib,
  stdenv,
  swift,
  makeWrapper,
  rcodesign,
}:

stdenv.mkDerivation {
  pname = "photos-import";
  version = "1.0";

  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./photos-import.swift
      ./Info.plist
    ];
  };

  nativeBuildInputs = [
    swift
    makeWrapper
    rcodesign
  ];
  dontConfigure = true;

  buildPhase = ''
    runHook preBuild
    swiftc -O -framework Photos -framework Foundation photos-import.swift -o photos-import
    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    app="$out/Applications/photos-import.app"
    mkdir -p "$app/Contents/MacOS" "$out/bin"
    install -m 644 Info.plist "$app/Contents/Info.plist"
    install -m 755 photos-import "$app/Contents/MacOS/photos-import"
    makeWrapper "$app/Contents/MacOS/photos-import" "$out/bin/photos-import"
    runHook postInstall
  '';

  # Sign the whole final bundle after stripping and other binary fixups, not
  # merely its Mach-O file: Info.plist and resources must also be sealed.
  # With no certificate/key options rcodesign creates an ad-hoc signature,
  # deriving the identifier from Info.plist, without host tools or identities.
  postFixup = ''
    rcodesign sign "$out/Applications/photos-import.app"
  '';

  meta = {
    description = "Import an image into Photos with add-only PhotoKit authorization";
    platforms = [ "aarch64-darwin" ];
    mainProgram = "photos-import";
  };
}
