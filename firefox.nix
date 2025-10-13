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
            widget-overflow-fixed-list = [ ];
            unified-extensions-area = [
              "open_in_iina_firefox_iina_io-browser-action"
              "openmultipleurls_ustat_de-browser-action"
              "link-extractor_cssnr_com-browser-action"
              "ublock0_raymondhill_net-browser-action"
              "vpn_proton_ch-browser-action"
              "_d7742d87-e61d-4b78-b8a1-b469842139fa_-browser-action"
              "chrome-gnome-shell_gnome_org-browser-action"
              "jid1-tsgsxbhncspbwq_jetpack-browser-action"
              "passbolt_passbolt_com-browser-action"
              "_testpilot-containers-browser-action"
              "password-manager-firefox-extension_apple_com-browser-action"
              "_3c078156-979c-498b-8990-85f7987dd929_-browser-action"
            ];
            nav-bar = [
              "back-button"
              "forward-button"
              "stop-reload-button"
              "vertical-spacer"
              "urlbar-container"
              "downloads-button"
              "unified-extensions-button"
              "addon_darkreader_org-browser-action"
              "_531906d3-e22f-4a6c-a102-8057b88a1a63_-browser-action"
            ];
            TabsToolbar = [
              "firefox-view-button"
              "tabbrowser-tabs"
              "new-tab-button"
              "alltabs-button"
            ];
            vertical-tabs = [ ];
            PersonalToolbar = [ "personal-bookmarks" ];
          };
          seen = [
            "open_in_iina_firefox_iina_io-browser-action"
            "openmultipleurls_ustat_de-browser-action"
            "link-extractor_cssnr_com-browser-action"
            "vpn_proton_ch-browser-action"
            "_d7742d87-e61d-4b78-b8a1-b469842139fa_-browser-action"
            "_testpilot-containers-browser-action"
            "addon_darkreader_org-browser-action"
            "chrome-gnome-shell_gnome_org-browser-action"
            "jid1-tsgsxbhncspbwq_jetpack-browser-action"
            "passbolt_passbolt_com-browser-action"
            "password-manager-firefox-extension_apple_com-browser-action"
            "ublock0_raymondhill_net-browser-action"
            "_3c078156-979c-498b-8990-85f7987dd929_-browser-action"
            "_531906d3-e22f-4a6c-a102-8057b88a1a63_-browser-action"
            "developer-button"
            "screenshot-button"
          ];
          dirtyAreaCache = [
            "unified-extensions-area"
            "nav-bar"
            "TabsToolbar"
            "vertical-tabs"
            "PersonalToolbar"
            "widget-overflow-fixed-list"
          ];
          currentVersion = 23;
          newElementCount = 7;
        };
      };
    };
  };
}
