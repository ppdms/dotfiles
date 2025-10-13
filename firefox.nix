{
  pkgs,
  config,
  systemConfig,
  ...
}:
{
  programs.firefox = {
    enable = true;
    # Use null to skip package installation (Firefox installed via Homebrew)
    package = null;
    profiles.default-release = {
      id = 0;
      search = {
        force = true;
        default = "google";
        privateDefault = "google";
        order = [ "google" ];
        engines = {
          bing.metaData.hidden = true;
        };
      };
      bookmarks = { };

      # Enable userChrome.css
      userChrome = builtins.readFile ./userChrome.css;

      settings = {
        "browser.startup.homepage" = "about:home";
        # Required to enable userChrome.css
        "toolkit.legacyUserProfileCustomizations.stylesheets" = true;

        # Disable irritating first-run stuff
        "browser.disableResetPrompt" = true;
        "browser.download.panel.shown" = true;
        "browser.feeds.showFirstRunUI" = false;
        "browser.messaging-system.whatsNewPanel.enabled" = false;
        "browser.rights.3.shown" = true;
        "browser.shell.checkDefaultBrowser" = false;
        "browser.shell.defaultBrowserCheckCount" = 1;
        "browser.startup.homepage_override.mstone" = "ignore";
        "browser.uitour.enabled" = false;
        "startup.homepage_override_url" = "";
        "trailhead.firstrun.didSeeAboutWelcome" = true;
        "browser.bookmarks.restore_default_bookmarks" = false;
        "browser.bookmarks.addedImportButton" = true;

        # Don't ask for download dir
        "browser.download.useDownloadDir" = false;

        # Disable crappy home activity stream page
        "browser.newtabpage.activity-stream.feeds.topsites" = false;
        "browser.newtabpage.activity-stream.showSponsoredTopSites" = false;
        "browser.newtabpage.activity-stream.improvesearch.topSiteSearchShortcuts" = false;
        "browser.newtabpage.blocked" = builtins.listToAttrs (
          map
            (name: {
              inherit name;
              value = 1;
            })
            [
              # Youtube
              "26UbzFJ7qT9/4DhodHKA1Q=="
              # Facebook
              "4gPpjkxgZzXPVtuEoAL9Ig=="
              # Wikipedia
              "eV8/WsSLxHadrTL1gAxhug=="
              # Reddit
              "gLv0ja2RYVgxKdp0I5qwvA=="
              # Amazon
              "K00ILysCaEq8+bEqV/3nuw=="
              # Twitter
              "T9nJot5PurhJSy8n038xGA=="
            ]
        );

        # Disable some telemetry
        "app.shield.optoutstudies.enabled" = false;
        "browser.discovery.enabled" = false;
        "browser.newtabpage.activity-stream.feeds.telemetry" = false;
        "browser.newtabpage.activity-stream.telemetry" = false;
        "browser.ping-centre.telemetry" = false;
        "datareporting.healthreport.service.enabled" = false;
        "datareporting.healthreport.uploadEnabled" = false;
        "datareporting.policy.dataSubmissionEnabled" = false;
        "datareporting.sessions.current.clean" = true;
        "devtools.onboarding.telemetry.logged" = false;
        "toolkit.telemetry.archive.enabled" = false;
        "toolkit.telemetry.bhrPing.enabled" = false;
        "toolkit.telemetry.enabled" = false;
        "toolkit.telemetry.firstShutdownPing.enabled" = false;
        "toolkit.telemetry.hybridContent.enabled" = false;
        "toolkit.telemetry.newProfilePing.enabled" = false;
        "toolkit.telemetry.prompted" = 2;
        "toolkit.telemetry.rejected" = true;
        "toolkit.telemetry.reportingpolicy.firstRun" = false;
        "toolkit.telemetry.server" = "";
        "toolkit.telemetry.shutdownPingSender.enabled" = false;
        "toolkit.telemetry.unified" = false;
        "toolkit.telemetry.unifiedIsOptIn" = false;
        "toolkit.telemetry.updatePing.enabled" = false;

        # Enable fx accounts for sync
        "identity.fxaccounts.enabled" = true;
        # Disable "save password" prompt
        "signon.rememberSignons" = false;
        # Harden
        "privacy.trackingprotection.enabled" = true;
        "dom.security.https_only_mode" = true;
        # Use native macOS title bar for compact layout
        "browser.tabs.inTitlebar" = 1;
        # Vertical tabs disabled
        "sidebar.verticalTabs" = false;
        "sidebar.revamp" = false;
        # Layout
        "browser.uiCustomization.state" = builtins.toJSON {
          placements = {
            unified-extensions-area = [ ];
            widget-overflow-fixed-list = [ ];
            nav-bar = [
              "back-button"
              "forward-button"
              "vertical-spacer"
              "stop-reload-button"
              "urlbar-container"
              "downloads-button"
              "ublock0_raymondhill_net-browser-action"
              "_testpilot-containers-browser-action"
              "reset-pbm-toolbar-button"
              "unified-extensions-button"
            ];
            toolbar-menubar = [ "menubar-items" ];
            TabsToolbar = [ ];
            vertical-tabs = [ "tabbrowser-tabs" ];
            PersonalToolbar = [ "personal-bookmarks" ];
          };
          seen = [
            "save-to-pocket-button"
            "developer-button"
            "ublock0_raymondhill_net-browser-action"
            "_testpilot-containers-browser-action"
            "screenshot-button"
          ];
          dirtyAreaCache = [
            "nav-bar"
            "PersonalToolbar"
            "toolbar-menubar"
            "TabsToolbar"
            "widget-overflow-fixed-list"
            "vertical-tabs"
          ];
          currentVersion = 23;
          newElementCount = 10;
        };
      };
    };
  };
}
