{
  lib,
  buildNpmPackage,
  nodejs,
  makeWrapper,
  libsecret,
  stdenv,
}:

# Full npm closure of the `cf` package (cf/package.json + package-lock.json),
# so `cf dev` / `--local` (miniflare + workerd) work, not just the bundled
# API commands. Update: bump `cf` in cf/package.json, regenerate the lockfile
# with `npm install --package-lock-only --prefix services/tools/cf`, then
# refresh npmDepsHash with `prefetch-npm-deps services/tools/cf/package-lock.json`.
buildNpmPackage {
  pname = "cloudflare-cf";
  version = "1.0.0-beta.17";

  src = lib.fileset.toSource {
    root = ./cf;
    fileset = lib.fileset.unions [
      ./cf/package.json
      ./cf/package-lock.json
    ];
  };

  npmDepsHash = "sha256-Lt3zFkfneo6GEjUhyKvgyoyrHJJVHS1Ft6ntyv2Dolc=";
  dontNpmBuild = true;

  nativeBuildInputs = [ makeWrapper ];

  installPhase = ''
    runHook preInstall
    mkdir -p "$out/lib/cf" "$out/bin"
    cp -r node_modules "$out/lib/cf/node_modules"
    makeWrapper ${nodejs}/bin/node "$out/bin/cf" \
      ${lib.optionalString stdenv.hostPlatform.isLinux "--prefix PATH : ${lib.makeBinPath [ libsecret ]}"} \
      --set CLOUDFLARE_AUTH_USE_KEYRING true \
      --add-flags "$out/lib/cf/node_modules/cf/bin/cf"
    ln -s "$out/bin/cf" "$out/bin/cloudflare"
    runHook postInstall
  '';

  meta = with lib; {
    description = "Cloudflare's current CLI covering the whole platform (cf init/dev/deploy)";
    homepage = "https://developers.cloudflare.com/cf";
    license = with licenses; [
      asl20
      mit
    ];
    mainProgram = "cf";
    platforms = platforms.darwin ++ platforms.linux;
  };
}
