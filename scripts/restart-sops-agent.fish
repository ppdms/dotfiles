#!/usr/bin/env fish
# Restart the SOPS SSH agent with proper environment variables

# Kill existing SOPS agent
pkill -f "ssh-agent.*sops-agent.sock"
rm -f ~/.ssh/sops-agent.sock

echo "SOPS agent stopped. It will restart automatically when you run SSH."
echo "Or start it now by running a new shell or: source ~/.config/fish/config.fish"
