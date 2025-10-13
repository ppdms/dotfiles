# Nix Configuration

This repository contains my personal Nix configuration files for macOS using home-manager and nix-darwin.

## 📁 Repository Structure

### Public Configuration (`public/`)

This directory contains all shareable configuration files that don't contain secrets:

- **`flake.nix`** - Main Nix flake configuration
- **`firefox.nix`** - Firefox browser configuration
- **`ssh-helpers.nix`** - Generic SSH helper functions (imports host-specific config from private/)
- **`userChrome.css`** - Firefox custom CSS
- **`vscode-settings.json`** - Visual Studio Code settings
- **`kitty/`** - Kitty terminal emulator configuration
  - `kitty.conf` - Main configuration
  - `Gruvbox_Dark_Hard.conf` - Color scheme
- **`scripts/`** - Utility scripts for secrets management and SSH setup
- **`vim-templates/`** - File templates for various programming languages
- **`private/`** - Template files for private configuration (see below)

### Private Configuration (`../private/`)

⚠️ **Not included in this repository** - Contains sensitive information and secrets.

The `private` folder should be created at `~/.config/nix/private/` (one level up from this public folder) and should contain:

- **`env.nix`** - Environment-specific settings (hostname, username, email, SSH host patterns, etc.)
- **`age/`** - Age encryption keys
  - `keys.txt` - Secure Enclave key for SOPS (protected by TouchID on macOS)
  - `backup-key.txt` - Backup age key (stored encrypted in secrets.yaml + kept safe offline)
- **`secrets/`** - Encrypted secrets directory
  - `secrets.yaml` - SOPS-encrypted secrets (SSH keys, tokens, etc.)
  - `.sops.yaml` - SOPS configuration
  - `backups/` - Timestamped backup archives encrypted with backup key only

## 🔒 Secrets Management

This configuration uses [SOPS (Secrets OPerationS)](https://github.com/mozilla/sops) with [age encryption](https://github.com/FiloSottile/age) to manage secrets securely.

## 🔑 Automated Backup System for Secrets

To enable portable, hardware-independent backups of your secrets (removing Secure Enclave limitations), this setup provides:

- A regular age backup key that can decrypt secrets on any system
- The backup key itself is stored encrypted in `secrets.yaml` (protected by your Secure Enclave key)
- A cleartext copy stored securely offline for emergency recovery
- A wrapper script that automatically rekeys and backs up your secrets every time you edit them

### How it works

1. Generate a backup age key (one-time setup)
2. Store the key encrypted in `secrets.yaml` using your Secure Enclave key
3. Keep a cleartext copy somewhere safe (password manager, encrypted USB, etc.)
4. Every time you edit secrets through the wrapper script:
   - Secrets are edited with SOPS (using Secure Enclave key)
   - File is rekeyed to be encrypted with BOTH keys (Secure Enclave + backup key)
   - A timestamped backup of the **decrypted plaintext** is created, encrypted with backup key only
5. In an emergency (lost Mac, hardware failure), use the backup key to decrypt any backup archive

### Setup Steps

**Quick start:** See `public/scripts/README.md` for step-by-step instructions.

**Usage:**

```fish
secrets  # (runs the wrapper script, edits and backs up automatically)
```

Backups are stored in `~/.config/nix/private/secrets/backups/` as `.age` files, each containing the full decrypted secrets encrypted with your backup key only.

### Key Features

- **On-demand decryption**: Secrets are decrypted only when needed, not at system activation
- **TouchID integration**: Using `age-plugin-se` for Secure Enclave storage on macOS
- **Dual SSH agent system**: 
  - Default agent (Secretive): For Secure Enclave keys stored in Secretive app
  - SOPS agent: For SOPS-encrypted keys, used automatically for specific hosts
- **Automatic SSH key management**: SSH wrapper detects configured hosts and auto-decrypts keys
- **Confirmation on every use**: SOPS agent requires explicit confirmation each time a key is used (like Secretive)
- **Security-first**: Keys persist in SOPS agent but require confirmation before each use

### How SSH Key Management Works

The configuration uses two SSH agents:

1. **Default SSH Agent (Secretive)**: 
   - Used for all hosts by default
   - Keys stored in macOS Secure Enclave via Secretive app
   - Socket: `/Users/basil/Library/Containers/com.maxgoedjen.Secretive.SecretAgent/Data/socket.ssh`

2. **SOPS SSH Agent**:
   - Used automatically for specific configured hosts (e.g., `*.example.net`, `ansible-*`)
   - Keys decrypted from SOPS secrets with TouchID prompt
   - Socket: `~/.ssh/sops-agent.sock`
   - Keys loaded with `-c` (confirm) flag for security

**Workflow for SOPS-encrypted SSH keys:**

1. **Pattern Detection**: SSH wrapper detects if hostname matches configured patterns
2. **Auto-decrypt**: If match found, decrypts SSH key from secrets.yaml (TouchID prompt)
3. **Key Loading**: Adds key to SOPS agent with `-c` flag (requires confirmation on use)
4. **Connection**: SSH connects using SOPS agent
5. **Confirmation Dialog**: Each time key is about to be used, system dialog prompts for confirmation
6. **Persistence**: Key remains in agent (no need to re-decrypt) but still requires confirmation for each use

**Security Note**: The `-c` flag makes SSH agent require confirmation at the agent level (OpenSSH feature), similar to how Secretive works. This requires `theseal/ssh-askpass` to provide the confirmation dialog on macOS. The binary is called automatically by ssh-agent when needed (no background service required).

**First-time setup**: After installing this configuration, reload your shell to get the required environment variables:
```bash
exec $SHELL
```
See `public/scripts/SETUP-SSH-ASKPASS.md` for complete setup instructions and troubleshooting.

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
   
   # Edit with your information (system config + SSH host patterns)
   $EDITOR ~/.config/nix/private/env.nix
   ```

4. **Create your secrets file and SOPS config**:
   ```bash
   # Create .sops.yaml in private/secrets/
   cd ~/.config/nix/private/secrets
   
   # Get your public key from keys.txt
   grep "public key:" ~/.config/nix/private/age/keys.txt
   
   # Create .sops.yaml
   cat > .sops.yaml << 'EOF'
   keys:
     - &admin_age YOUR_AGE_PUBLIC_KEY_HERE
   creation_rules:
     - path_regex: .*\.yaml$
       key_groups:
         - age:
             - *admin_age
   EOF
   
   # Create and edit secrets file
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

6. **Update env.nix** to match your SSH hosts and secret names:
   - Change host patterns in `ssh.zsh.hostPatternCheck` and `ssh.fish.hostPatternCheck` (e.g., `\.example\.com$` to your domain)
   - Update `ssh.secretName` to match the key name in your secrets.yaml
   - Update `ssh.keyComment` to match your SSH key's comment
   - Configure `ssh.sopsHosts` with your host patterns
   - Update `ssh.defaultIdentityAgent` path to match your username

7. **(Optional) Set up backup key** for portable, hardware-independent backups:
   ```bash
   # Generate backup key
   age-keygen -o ~/.config/nix/private/age/backup-key.txt
   
   # Run setup script (updates .sops.yaml, guides you through adding key to secrets.yaml)
   bash ~/.config/nix/public/scripts/setup-backup-key.sh
   
   # Store backup-key.txt somewhere safe (password manager, encrypted USB, etc.)
   ```
   
   See `public/scripts/BACKUP-QUICKSTART.md` for detailed backup setup instructions.

8. **Add private directory to .gitignore**:
   ```bash
   echo "private/" >> ~/.config/nix/.gitignore
   ```

## 🚀 Usage

### Shell Commands

The configuration provides several helpful commands:

- **`sops-list`** - List keys currently loaded in the SOPS SSH agent
- **`sops-clear`** - Clear all keys from the SOPS SSH agent
- **`ssh-with-agent <secret_name> [ssh args...]`** - Manually SSH with a specific decrypted key (adds, uses, then immediately removes)
- **`sops-ensure-key <secret_name>`** - Load a key into the SOPS agent (persists with confirmation required)

### Automatic SSH Key Management

The SSH wrapper automatically handles key decryption for configured hosts:

```bash
# For hosts matching configured patterns (e.g., *.example.net):
# 1. Detects the hostname matches your configured pattern
# 2. Prompts for TouchID to decrypt the key (first time only)
# 3. Loads the key into SOPS agent with confirmation required (-c flag)
# 4. Connects using SOPS agent
# 5. Prompts for confirmation each time the key is about to be used
ssh user@host.example.net

# For all other hosts:
# Uses default Secretive agent (Secure Enclave keys)
ssh user@github.com
```

**Key persistence**: Once loaded, SOPS keys remain in the agent (no need to re-enter TouchID), but you must confirm each use via system dialog. Use `sops-clear` to remove keys when done.

### Manual Secret Decryption

To manually decrypt a secret from `secrets.yaml`:

```bash
# View a specific secret
sops -d --extract '["secret_name"]' ~/.config/nix/private/secrets/secrets.yaml

# Edit secrets file (recommended: use the backup wrapper)
secrets  # This also creates automatic backups

# Or edit directly (no backup)
cd ~/.config/nix/private/secrets
sops secrets.yaml
```

### Emergency Recovery

If you lose access to your Secure Enclave key (new Mac, hardware failure):

```bash
# Decrypt from backup using your backup key
age --decrypt -i /path/to/backup-key.txt \
  ~/.config/nix/private/secrets/backups/secrets.yaml.TIMESTAMP.age > secrets.yaml

# Or decrypt and view
age --decrypt -i /path/to/backup-key.txt backup-file.age | less
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

# Reload shell to get new environment variables
exec $SHELL
```

## 🔐 Security Best Practices

1. **Never commit secrets**: Ensure `private/` is in `.gitignore`
2. **Use strong encryption**: The age keys are protected by TouchID/Secure Enclave
3. **Regular rotation**: Periodically rotate SSH keys and tokens
4. **Backup safely**: 
   - Keep your `backup-key.txt` in a secure location (password manager, encrypted USB)
   - Backup archives in `backups/` directory can be stored anywhere (they're encrypted)
   - Consider keeping backup archives in cloud storage for redundancy
5. **Audit access**: Review `sops-list` to see what keys are loaded in SOPS agent
6. **Clean up**: 
   - SOPS keys persist in agent (for convenience) but require confirmation on each use
   - Use `sops-clear` to remove keys from SOPS agent when done
   - Secretive keys are managed by the Secretive app
7. **Test recovery**: Periodically test decrypting a backup to ensure your backup key works
8. **Confirmation prompts**: Never blindly accept SSH key confirmation dialogs - verify the operation is expected

## 📚 Resources

- [Nix Manual](https://nixos.org/manual/nix/stable/)
- [home-manager](https://github.com/nix-community/home-manager)
- [nix-darwin](https://github.com/LnL7/nix-darwin)
- [SOPS](https://github.com/mozilla/sops)
- [age encryption](https://github.com/FiloSottile/age)
- [age-plugin-se](https://github.com/remko/age-plugin-se)

## 📄 License

This configuration is personal and provided as-is for reference. Feel free to use any part of it for your own configuration.
