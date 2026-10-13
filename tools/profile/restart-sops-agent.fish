#!/usr/bin/env fish
# Restart the SOPS SSH agent service and reload the decrypted SSH keys.

set -l sock "$HOME/.ssh/sops-agent.sock"

# ssh-add -l exit codes: 0 = agent reachable with keys, 1 = agent reachable
# without keys, 2 = agent not reachable. Returns 0 when the agent at the
# given socket responds.
function __agent_ok
    env SSH_AUTH_SOCK="$argv[1]" ssh-add -l >/dev/null 2>&1
    or test $status -eq 1
end

if test (uname -s) = Darwin
    # Darwin - use launchd
    echo "Restarting SOPS SSH agent (launchd service)..."

    # A failed previous agent can leave its endpoint behind. Never remove
    # the socket of an agent that still responds.
    if test -S "$sock"
        if not __agent_ok "$sock"
            echo "Removing stale socket..."
            rm -f "$sock"
        end
    end

    if launchctl list | grep -q org.nix-community.home.sops-ssh-agent
        launchctl kickstart -k gui/(id -u)/org.nix-community.home.sops-ssh-agent
    else
        echo "Service will be loaded by home-manager in the next activation step..."
    end

    # Wait up to ~10s for the socket to appear
    for i in (seq 1 20)
        if test -S "$sock"
            break
        end
        sleep 0.5
    end

    if test -S "$sock"
        if __agent_ok "$sock"
            # The restart wiped the agent; reload the decrypted keys.
            if launchctl list | grep -q org.nix-community.home.sops-load-key
                launchctl kickstart gui/(id -u)/org.nix-community.home.sops-load-key
            end
            # The loader is asynchronous; wait until it has finished adding
            # the decrypted key before reporting a successful restart.
            set -l key_loaded false
            for i in (seq 1 20)
                if env SSH_AUTH_SOCK="$sock" ssh-add -l >/dev/null 2>&1
                    set key_loaded true
                    break
                end
                sleep 0.5
            end
            if test "$key_loaded" != true
                echo "✗ Error: SOPS key loader did not load a key"
                exit 1
            end
            echo "✓ SOPS SSH agent started successfully"
        else
            echo "✗ Error: SOPS agent socket exists but agent is not responsive"
            launchctl list | grep sops
            exit 1
        end
    else
        # No socket. If we kicked the agent, that is a real failure; if it was
        # not loaded yet, home-manager will start it right after this step.
        if launchctl list | grep -q org.nix-community.home.sops-ssh-agent
            echo "✗ Error: Failed to create SOPS agent socket"
            launchctl list | grep sops
            exit 1
        else
            echo "SOPS agent not loaded yet; home-manager activation will start it"
        end
    end
else
    echo "Error: This helper manages the Darwin SOPS SSH agent only"
    exit 1
end
