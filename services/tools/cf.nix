{
  lib,
  stdenvNoCC,
  fetchurl,
  nodejs,
  makeWrapper,
  libsecret,
}:

let
  minisearch = fetchurl {
    url = "https://registry.npmjs.org/minisearch/-/minisearch-7.2.0.tgz";
    hash = "sha256-yzuBJqPqZdazh3hylPB5Kw6kpAtw+PN2iAZqVjjgIYo=";
  };

in
stdenvNoCC.mkDerivation {
  pname = "cloudflare-cf";
  version = "1.0.0-beta.12";

  src = fetchurl {
    url = "https://registry.npmjs.org/cf/-/cf-1.0.0-beta.12.tgz";
    hash = "sha256-LGaN+SuptzxQq1uElbwA71HzBk3en/cLPZwCyOTLMHY=";
  };

  nativeBuildInputs = [ makeWrapper ];

  installPhase = ''
    runHook preInstall
    mkdir -p "$out/lib/cf/node_modules/minisearch" "$out/bin"
    cp -r bin dist package.json README.md LICENSE-APACHE LICENSE-MIT "$out/lib/cf/"
    tar xzf ${minisearch} -C "$out/lib/cf/node_modules/minisearch" --strip-components=1
    makeWrapper ${nodejs}/bin/node "$out/bin/cf" \
      --prefix PATH : ${lib.makeBinPath [ libsecret ]} \
      --set CLOUDFLARE_AUTH_USE_KEYRING true \
      --add-flags "$out/lib/cf/bin/cf"
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
    platforms = platforms.linux;
  };
}
