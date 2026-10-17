{ lib, systemConfig, ... }:
{
  programs.git = {
    enable = true;
    settings = {
      user = {
        name = systemConfig.user.fullName;
        email = systemConfig.user.email;
      };
      init.defaultBranch = "main";
      push.autoSetupRemote = true;
      alias.now = "!f() { git add . && git commit -a -m \"$(date +%s)\"; }; f";
      credential = {
        "https://github.com".helper = [
          ""
          "!gh auth git-credential"
        ];
        "https://gist.github.com".helper = [
          ""
          "!gh auth git-credential"
        ];
      };
    }
    // lib.optionalAttrs (systemConfig.user.gitSigningKey != null) {
      gpg.format = "ssh";
      "gpg \"ssh\"".allowedSignersFile = "~/.gitallowedsigners";
    };
    ignores = [ ".DS_Store" ];
    signing = {
      key = systemConfig.user.gitSigningKey;
      signByDefault = systemConfig.user.signCommits;
    };
  };

  home.file.".gitallowedsigners" = lib.mkIf (systemConfig.user.gitSigningKey != null) {
    text = ''
      ${systemConfig.user.email} ${systemConfig.user.gitSigningKey}
    '';
  };
}
