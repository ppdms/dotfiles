# Nix Configuration

This repository contains my personal Nix configuration files for macOS using home-manager and nix-darwin.

## 📁 Repository Structure

### Public Configuration (`public/`)

This directory contains all shareable configuration files that don't contain secrets:

- **`flake.nix`** - Main Nix flake configuration
- **`flake.lock`** - Locked dependencies for reproducibility
- **`firefox.nix`** - Firefox browser configuration
- **`userChrome.css`** - Firefox custom CSS
- **`vscode-settings.json`** - Visual Studio Code settings
- **`kitty/`** - Kitty terminal emulator configuration
  - `kitty.conf` - Main configuration
  - `Gruvbox_Dark_Hard.conf` - Color scheme
- **`vim-templates/`** - File templates for various programming languages
- **`private/`** - Template files for private configuration (see below)

### Private Configuration (`../private/`)

⚠️ **Not included in this repository** - Contains sensitive information and secrets.

The `private` folder should be created at `~/.config/nix/private/` (one level up from this public folder) and should contain:

- **`github-token.nix`** - GitHub personal access token configuration
- **`system-config.nix`** - System-specific settings (hostname, username, email, etc.)
- **`ssh-helpers.nix`** - SSH wrapper functions with SOPS integration
- **`age/keys.txt`** - Age encryption key for SOPS (protected by TouchID on macOS)
- **`secrets/`** - Encrypted secrets directory
  - `secrets.yaml` - SOPS-encrypted secrets (SSH keys, tokens, etc.)
  - `sops-config.nix` - SOPS configuration

## 🔒 Secrets Management

This configuration uses [SOPS (Secrets OPerationS)](https://github.com/mozilla/sops) with [age encryption](https://github.com/FiloSottile/age) to manage secrets securely.

### Key Features

- **On-demand decryption**: Secrets are decrypted only when needed, not at system activation
- **TouchID integration**: Using `age-plugin-se` for Secure Enclave storage on macOS
- **Automatic SSH key management**: SSH wrapper automatically decrypts and loads keys for specific hosts
- **Security-first**: Keys are removed from SSH agent immediately after use

### How It Works

1. **Encryption**: Secrets are encrypted using age with a key stored in the Secure Enclave
2. **SSH Wrapper**: Custom shell functions intercept SSH connections to specific hosts
3. **Auto-decrypt**: When connecting to a configured host, the SSH key is decrypted (with TouchID prompt)
4. **Temporary Loading**: Key is added to a separate SSH agent (`~/.ssh/sops-agent.sock`)
5. **Auto-cleanup**: Key is removed from agent immediately after SSH connection closes

### Setting Up Your Own Private Configuration

1. **Create the private directory structure**:
   ```bash
   mkdir -p ~/.config/nix/private/{age,secrets}
   ```

2. **Generate an age key** (with Secure Enclave on macOS):
   ```bash
   # Install age-plugin-se
   nix-shell -p age-plugin-se
   
   # Generate key (will prompt for TouchID setup)
   age-plugin-se -o ~/.config/nix/private/age/keys.txt
   ```

3. **Copy and customize template files**:
   ```bash
   # Copy templates from public/private/ to ../private/
   cp -r ~/.config/nix/public/private/* ~/.config/nix/private/
   
   # Edit with your information
   $EDITOR ~/.config/nix/private/system-config.nix
   $EDITOR ~/.config/nix/private/ssh-helpers.nix
   ```

4. **Create your secrets file**:
   ```bash
   # Create .sops.yaml in ~/.config/nix/
   cat > ~/.config/nix/.sops.yaml << 'EOF'
   keys:
     - &admin_age YOUR_AGE_PUBLIC_KEY_HERE
   creation_rules:
     - path_regex: private/secrets/secrets.yaml$
       key_groups:
         - age:
             - *admin_age
   EOF
   
   # Get your public key from keys.txt (the line starting with "# public key:")
   grep "public key:" ~/.config/nix/private/age/keys.txt
   
   # Create and edit secrets file
   cd ~/.config/nix/private/secrets
   sops secrets.yaml
   ```

5. **Add your secrets** in the SOPS editor:
   ```yaml
   # Example structure
   your_ssh_key_name: |
     -----BEGIN OPENSSH PRIVATE KEY-----
     ... your actual SSH private key ...
     -----END OPENSSH PRIVATE KEY-----
   github_token: ghp_yourActualGitHubToken
   ```

6. **Update ssh-helpers.nix** to match your hosts and secret names:
   - Change host patterns (e.g., `\.example\.com$` to your domain)
   - Update secret names (e.g., `your_ssh_key_name` to match your secrets.yaml keys)

7. **Add private directory to .gitignore**:
   ```bash
   echo "private/" >> ~/.config/nix/.gitignore
   ```

## 🚀 Usage

### Shell Commands

The configuration provides several helpful commands:

- **`sops-list`** - List keys currently loaded in the SOPS SSH agent
- **`sops-clear`** - Clear all keys from the SOPS SSH agent
- **`ssh-with-agent <secret_name> [ssh args...]`** - Manually SSH with a specific decrypted key
- **`sops-ensure-key <secret_name>`** - Load a key into the SOPS agent (with 5-minute timeout)

### Automatic SSH Key Management

The SSH wrapper automatically handles key decryption for configured hosts:

```bash
# This will automatically:
# 1. Detect it matches your configured pattern
# 2. Prompt for TouchID to decrypt the key
# 3. Load the key into SOPS agent
# 4. Run SSH command
# 5. Remove key from agent after connection closes
ssh user@your-configured-host.example.com
```

### Manual Secret Decryption

To manually decrypt a secret from `secrets.yaml`:

```bash
# View a specific secret
sops -d --extract '["secret_name"]' ~/.config/nix/private/secrets/secrets.yaml

# Edit secrets file
cd ~/.config/nix/private/secrets
sops secrets.yaml
```

## 📋 Template Files

The `private/` directory in this repository contains template files showing the structure of private configuration files. These templates:

- ✅ Show the correct file structure and format
- ✅ Include helpful comments and documentation
- ✅ Contain placeholder values
- ❌ Do NOT contain any actual secrets or personal information

Copy these templates to `~/.config/nix/private/` and customize them with your own information.

## 🔧 Development Tools

### Vim Templates

The `vim-templates/` directory contains templates for quick file creation in Vim:

- Programming languages: C, C++, Python, JavaScript, Java, etc.
- Build systems: Makefile, CMakeLists.txt
- Web: HTML, JSX
- Testing: Mock files

Templates are automatically loaded in Vim when creating new files with the corresponding extension.

## 📦 Installation

```bash
# Clone the repository
git clone <your-repo-url> ~/.config/nix/public

# Set up private configuration (see "Setting Up Your Own Private Configuration" above)
mkdir -p ~/.config/nix/private

# Copy and customize templates
cp -r ~/.config/nix/public/private/* ~/.config/nix/private/
# ... edit private files with your information ...

# Build and activate configuration
nix build ~/.config/nix/public#darwinConfigurations.YOUR-HOSTNAME.system
./result/sw/bin/darwin-rebuild switch --flake ~/.config/nix/public
```

## 🔐 Security Best Practices

1. **Never commit secrets**: Ensure `private/` is in `.gitignore`
2. **Use strong encryption**: The age keys are protected by TouchID/Secure Enclave
3. **Regular rotation**: Periodically rotate SSH keys and tokens
4. **Backup safely**: Keep encrypted backups of `private/age/keys.txt`
5. **Audit access**: Review `sops-list` to see what keys are loaded
6. **Clean up**: Keys are automatically removed from agent after SSH sessions

## 🛠️ Troubleshooting

### SSH key not being decrypted

1. Check if your host pattern matches in `ssh-helpers.nix`
2. Verify the secret name matches what's in `secrets.yaml`
3. Ensure age key file exists and is readable: `~/.config/nix/private/age/keys.txt`

### TouchID not prompting

1. Verify age-plugin-se is installed: `which age-plugin-se`
2. Check if the key was created with Secure Enclave support
3. Test decryption manually: `sops -d ~/.config/nix/private/secrets/secrets.yaml`

### SOPS agent not starting

1. Check if socket exists: `ls -la ~/.ssh/sops-agent.sock`
2. Kill and restart: `pkill ssh-agent; exec $SHELL`
3. Verify SSH_AUTH_SOCK is set correctly in your shell

## 📚 Resources

- [Nix Manual](https://nixos.org/manual/nix/stable/)
- [home-manager](https://github.com/nix-community/home-manager)
- [nix-darwin](https://github.com/LnL7/nix-darwin)
- [SOPS](https://github.com/mozilla/sops)
- [age encryption](https://github.com/FiloSottile/age)
- [age-plugin-se](https://github.com/remko/age-plugin-se)

## 📄 License

This configuration is personal and provided as-is for reference. Feel free to use any part of it for your own configuration.
