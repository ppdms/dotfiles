{
  lib,
  stdenvNoCC,
  python3,
  makeWrapper,
  sops,
  age,
  age-plugin-se,
  git,
  openssl,
  systemd ? null,
}:
let
  python = python3.withPackages (packages: [ packages.pyyaml ]);
  runtime = [
    sops
    age
    git
    openssl
  ]
  ++ lib.optionals stdenvNoCC.hostPlatform.isLinux [ systemd ]
  ++ lib.optionals stdenvNoCC.hostPlatform.isDarwin [ age-plugin-se ];
in
stdenvNoCC.mkDerivation {
  pname = "profile";
  version = "1";
  dontUnpack = true;
  nativeBuildInputs = [ makeWrapper ];
  installPhase = ''
    runHook preInstall
    mkdir -p "$out/share/profile-tools/scripts" "$out/share/profile-tools/settings" "$out/bin"
    install -m 644 ${./profile.py} "$out/share/profile-tools/scripts/profile.py"
    install -m 644 ${../../settings/profile-schema.json} "$out/share/profile-tools/settings/profile-schema.json"
    install -m 644 ${../../settings/load-profile.nix} "$out/share/profile-tools/settings/load-profile.nix"
    makeWrapper ${python}/bin/python3 "$out/bin/profile" \
      --add-flags "$out/share/profile-tools/scripts/profile.py" \
      --set PROFILE_HELPER_PYTHON "$out/share/profile-tools/scripts/profile.py" \
      --prefix PATH : ${lib.makeBinPath runtime}
    makeWrapper "$out/bin/profile" "$out/bin/render-profile" --add-flags render
    makeWrapper "$out/bin/profile" "$out/bin/seal-profile" --add-flags seal
    makeWrapper "$out/bin/profile" "$out/bin/profile-edit" --add-flags edit
    runHook postInstall
  '';
  meta = {
    description = "Encrypts machine settings and prepares Nix build inputs";
    platforms = lib.platforms.linux ++ lib.platforms.darwin;
    mainProgram = "profile";
  };
}
