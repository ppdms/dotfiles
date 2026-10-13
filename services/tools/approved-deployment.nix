{
  runCommand,
  nix,
  jq,
  lib,
  boilerplate,
  profile,
  nixpkgsInput,
  homeManagerInput,
  nixDarwinInput,
  sopsNixInput,
}:
let
  sourceInfo = input: input.sourceInfo or input;
  nixString = value: lib.replaceStrings [ "\${" ] [ "\\\${" ] (builtins.toJSON value);
  vendor = input: owner: repo: ref: {
    locked = {
      type = "github";
      inherit owner repo;
      inherit (sourceInfo input) rev;
    }
    // lib.optionalAttrs (sourceInfo input ? lastModified) {
      inherit (sourceInfo input) lastModified;
    }
    // lib.optionalAttrs (sourceInfo input ? narHash) {
      inherit (sourceInfo input) narHash;
    };
    original = {
      type = "github";
      inherit owner repo;
    }
    // lib.optionalAttrs (ref != null) { inherit ref; };
  };
  pathInput = path: {
    locked = {
      type = "path";
      inherit path;
    };
    original = {
      type = "path";
      inherit path;
    };
  };
  lock = {
    version = 7;
    root = "root";
    nodes = {
      root.inputs = {
        boilerplate = "boilerplate";
        profile = "profile";
        nixpkgs = "nixpkgs";
        home-manager = "home-manager";
        nix-darwin = "nix-darwin";
        sops-nix = "sops-nix";
      };
      boilerplate = (pathInput boilerplate.outPath) // {
        inputs = {
          nixpkgs = [ "nixpkgs" ];
          home-manager = [ "home-manager" ];
          nix-darwin = [ "nix-darwin" ];
          sops-nix = [ "sops-nix" ];
          profile = [ "profile" ];
        };
      };
      profile = pathInput profile.outPath;
      nixpkgs = vendor nixpkgsInput "NixOS" "nixpkgs" "nixos-26.05";
      home-manager = (vendor homeManagerInput "nix-community" "home-manager" "release-26.05") // {
        inputs.nixpkgs = [ "nixpkgs" ];
      };
      nix-darwin = (vendor nixDarwinInput "nix-darwin" "nix-darwin" "nix-darwin-26.05") // {
        inputs.nixpkgs = [ "nixpkgs" ];
      };
      sops-nix = (vendor sopsNixInput "Mic92" "sops-nix" null) // {
        inputs.nixpkgs = [ "nixpkgs" ];
      };
    };
  };
  flake = ''
    {
      description = "Root-approved immutable server deployment";
      inputs = {
        nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
        home-manager = {
          url = "github:nix-community/home-manager/release-26.05";
          inputs.nixpkgs.follows = "nixpkgs";
        };
        nix-darwin = {
          url = "github:nix-darwin/nix-darwin/nix-darwin-26.05";
          inputs.nixpkgs.follows = "nixpkgs";
        };
        sops-nix = {
          url = "github:Mic92/sops-nix";
          inputs.nixpkgs.follows = "nixpkgs";
        };
        boilerplate = {
          url = ${nixString "path:${boilerplate.outPath}"};
          inputs.nixpkgs.follows = "nixpkgs";
          inputs.home-manager.follows = "home-manager";
          inputs.nix-darwin.follows = "nix-darwin";
          inputs.sops-nix.follows = "sops-nix";
          inputs.profile.follows = "profile";
        };
        profile.url = ${nixString "path:${profile.outPath}"};
      };
      outputs = { boilerplate, profile, ... }:
        boilerplate.lib.machines profile;
    }
  '';
in
runCommand "approved-server-deployment"
  {
    nativeBuildInputs = [
      nix
      jq
    ];
    flakeText = flake;
    lockText = builtins.toJSON lock;
    # Retain all active sources in the artifact closure, including vendors whose
    # lock entries contain revisions rather than store paths.
    sourceReferences = builtins.toJSON [
      boilerplate.outPath
      profile.outPath
      nixpkgsInput.outPath
      homeManagerInput.outPath
      nixDarwinInput.outPath
      sopsNixInput.outPath
    ];
    passAsFile = [
      "flakeText"
      "lockText"
      "sourceReferences"
    ];
  }
  ''
    mkdir "$out"
    cp "$flakeTextPath" "$out/flake.nix"
    cp "$sourceReferencesPath" "$out/source-references.json"
    boilerplateHash=$(nix --extra-experimental-features nix-command hash path --sri ${lib.escapeShellArg (toString boilerplate.outPath)})
    profileHash=$(nix --extra-experimental-features nix-command hash path --sri ${lib.escapeShellArg (toString profile.outPath)})
    nixpkgsHash=${
      if sourceInfo nixpkgsInput ? narHash then
        lib.escapeShellArg (sourceInfo nixpkgsInput).narHash
      else
        "$(nix --extra-experimental-features nix-command hash path --sri ${lib.escapeShellArg (toString nixpkgsInput.outPath)})"
    }
    homeManagerHash=${
      if sourceInfo homeManagerInput ? narHash then
        lib.escapeShellArg (sourceInfo homeManagerInput).narHash
      else
        "$(nix --extra-experimental-features nix-command hash path --sri ${lib.escapeShellArg (toString homeManagerInput.outPath)})"
    }
    nixDarwinHash=${
      if sourceInfo nixDarwinInput ? narHash then
        lib.escapeShellArg (sourceInfo nixDarwinInput).narHash
      else
        "$(nix --extra-experimental-features nix-command hash path --sri ${lib.escapeShellArg (toString nixDarwinInput.outPath)})"
    }
    sopsNixHash=${
      if sourceInfo sopsNixInput ? narHash then
        lib.escapeShellArg (sourceInfo sopsNixInput).narHash
      else
        "$(nix --extra-experimental-features nix-command hash path --sri ${lib.escapeShellArg (toString sopsNixInput.outPath)})"
    }
    jq --arg boilerplate "$boilerplateHash" --arg profile "$profileHash" \
       --arg nixpkgs "$nixpkgsHash" --arg homeManager "$homeManagerHash" \
       --arg nixDarwin "$nixDarwinHash" --arg sopsNix "$sopsNixHash" '
      .nodes.boilerplate.locked.narHash = $boilerplate |
      .nodes.profile.locked.narHash = $profile |
      .nodes.nixpkgs.locked.narHash //= $nixpkgs |
      .nodes["home-manager"].locked.narHash //= $homeManager |
      .nodes["nix-darwin"].locked.narHash //= $nixDarwin |
      .nodes["sops-nix"].locked.narHash //= $sopsNix
    ' "$lockTextPath" > "$out/flake.lock"
  ''
