# If you come from bash you might have to change your $PATH.
# export PATH=$HOME/bin:/usr/local/bin:$PATH

# Path to your oh-my-zsh installation.
export ZSH="$HOME/.oh-my-zsh"

# Set name of the theme to load --- if set to "random", it will
# load a random theme each time oh-my-zsh is loaded, in which case,
# to know which specific one was loaded, run: echo $RANDOM_THEME
# See https://github.com/ohmyzsh/ohmyzsh/wiki/Themes
ZSH_THEME="jreese"

# Set list of themes to pick from when loading at random
# Setting this variable when ZSH_THEME=random will cause zsh to load
# a theme from this variable instead of looking in $ZSH/themes/
# If set to an empty array, this variable will have no effect.
# ZSH_THEME_RANDOM_CANDIDATES=( "robbyrussell" "agnoster" )

# Uncomment the following line to use case-sensitive completion.
# CASE_SENSITIVE="true"

# Uncomment the following line to use hyphen-insensitive completion.
# Case-sensitive completion must be off. _ and - will be interchangeable.
# HYPHEN_INSENSITIVE="true"

# Uncomment one of the following lines to change the auto-update behavior
# zstyle ':omz:update' mode disabled  # disable automatic updates
# zstyle ':omz:update' mode auto      # update automatically without asking
# zstyle ':omz:update' mode reminder  # just remind me to update when it's time

# Uncomment the following line to change how often to auto-update (in days).
# zstyle ':omz:update' frequency 13

# Uncomment the following line if pasting URLs and other text is messed up.
# DISABLE_MAGIC_FUNCTIONS="true"

# Uncomment the following line to disable colors in ls.
# DISABLE_LS_COLORS="true"

# Uncomment the following line to disable auto-setting terminal title.
DISABLE_AUTO_TITLE="true"

# Uncomment the following line to enable command auto-correction.
# ENABLE_CORRECTION="true"

# Uncomment the following line to display red dots whilst waiting for completion.
# You can also set it to another string to have that shown instead of the default red dots.
# e.g. COMPLETION_WAITING_DOTS="%F{yellow}waiting...%f"
# Caution: this setting can cause issues with multiline prompts in zsh < 5.7.1 (see #5765)
COMPLETION_WAITING_DOTS="true"

# Uncomment the following line if you want to disable marking untracked files
# under VCS as dirty. This makes repository status check for large repositories
# much, much faster.
# DISABLE_UNTRACKED_FILES_DIRTY="true"

# Uncomment the following line if you want to change the command execution time
# stamp shown in the history command output.
# You can set one of the optional three formats:
# "mm/dd/yyyy"|"dd.mm.yyyy"|"yyyy-mm-dd"
# or set a custom format using the strftime function format specifications,
# see 'man strftime' for details.
# HIST_STAMPS="mm/dd/yyyy"

# Would you like to use another custom folder than $ZSH/custom?
# ZSH_CUSTOM=/path/to/new-custom-folder

# Which plugins would you like to load?
# Standard plugins can be found in $ZSH/plugins/
# Custom plugins may be added to $ZSH_CUSTOM/plugins/
# Example format: plugins=(rails git textmate ruby lighthouse)
# Add wisely, as too many plugins slow down shell startup.
plugins=()

source $ZSH/oh-my-zsh.sh

# User configuration

# export MANPATH="/usr/local/man:$MANPATH"

# You may need to manually set your language environment
# export LANG=en_US.UTF-8

# Preferred editor for local and remote sessions
# if [[ -n $SSH_CONNECTION ]]; then
#   export EDITOR='vim'
# else
#   export EDITOR='mvim'
# fi

# Compilation flags
# export ARCHFLAGS="-arch x86_64"

# Set personal aliases, overriding those provided by oh-my-zsh libs,
# plugins, and themes. Aliases can be placed here, though oh-my-zsh
# users are encouraged to define aliases within the ZSH_CUSTOM folder.
# For a full list of active aliases, run `alias`.
#
# Example aliases
# alias zshconfig="mate ~/.zshrc"
#alias ssync="/opt/homebrew/bin/python3 /Users/basil/uni/general/sync/sync.py"

export CFLAGS="-Wall -Wextra -Werror -O2 -std=c99 -pedantic"
export CXXFLAGS="-Wall -Wextra -Wconversion -Wnon-virtual-dtor -O3 -std=c++17"
# -pedantic

#if command -v tmux &> /dev/null && [ -n "$PS1" ] && [[ ! "$TERM" =~ screen ]] && [[ ! "$TERM" =~ tmux ]] && [ -z "$TMUX" ]; then
#  exec tmux
#fi

#alias g++='eval /opt/homebrew/bin/g++-13 $CXXFLAGS'
#alias clang++=g++

autoload -U colors && colors
#if command -v scutil &> /dev/null
#then
#        if [[ $(scutil --get LocalHostName) = 'Macintosh' ]]; then PS1="%F{015}%K{000}%% %{$reset_color%}" fi
#fi
#todaysWorkspace="/Users/basil/sandbox/$(date +'%Y%m%d')"
#if [ ! -d $todaysWorkspace ]; then
#  mkdir $todaysWorkspace
#fi
#cd $todaysWorkspace

function toRaspberry() {scp -r /Users/basil/$1 basil@192.168.1.30:/home/basil/}
function fromRaspberry() {scp -r basil@192.168.1.30:/home/basil/$1 /Users/basil/Desktop/}

alias s="kitten ssh"
alias dev="cd /Users/basil/Developer"
alias klar="clear && printf '\e[3J'"
alias bus="curl -s -A \"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36\" http://telematics.oasa.gr/api/\?act\=getStopArrivals\&p1\=380042 | jq -r '.[0].btime2' | figlet"

export LANG="en_US.UTF-8"
export LC_ALL="en_US.UTF-8"
export LC_CTYPE="en_US.UTF-8"
export LANG_ALL="en_US.UTF-8"

#alias emacs="emacs -nw"
#export PATH="/opt/homebrew/opt/openjdk/bin:$PATH"
#export PATH="/Users/basil/.config/emacs/bin:$PATH"

# >>> conda initialize >>>
# !! Contents within this block are managed by 'conda init' !!
__conda_setup="$('/opt/homebrew/anaconda3/bin/conda' 'shell.zsh' 'hook' 2> /dev/null)"
if [ $? -eq 0 ]; then
    eval "$__conda_setup"
else
    if [ -f "/opt/homebrew/anaconda3/etc/profile.d/conda.sh" ]; then
        . "/opt/homebrew/anaconda3/etc/profile.d/conda.sh"
    else
        export PATH="/opt/homebrew/anaconda3/bin:$PATH"
    fi
fi
unset __conda_setup
# <<< conda initialize <<<


# These shell commands are for moOde/MPD
alias mpd_pause="curl -X POST http://192.168.1.10/command/\?cmd=pause"
alias mpd_mute="curl -X POST http://192.168.1.10/command/\?cmd=vol.sh%20mute"
alias mpd_vol_up="curl -X POST http://192.168.1.10/command/\?cmd=vol.sh%20up%201"
alias mpd_vol_down="curl -X POST http://192.168.1.10/command/\?cmd=vol.sh\%20dn\%201"


# Function to set DNS servers
dns() {
  local network_service="Wi-Fi"
  case $1 in
    pihole)
      networksetup -setdnsservers $network_service 192.168.1.30
      ;;
    quad11)
      networksetup -setdnsservers $network_service 9.9.9.11
      ;;
    *)
      echo "Invalid argument. Usage: dns pihole or dns quad11"
      return 1
      ;;
  esac

  echo "DNS set to $1"
}


fortune | cowsay

eval "$(starship init zsh)"
