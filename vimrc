if (match(system("hostname -s"), 'Macintosh') >= 0)
        call plug#begin()
        Plug 'ActivityWatch/aw-watcher-vim'
        call plug#end()
endif
set nocompatible
filetype plugin indent on
syntax on
set expandtab
set ai
set number
set hlsearch
set ruler
set backspace=indent,eol,start
highlight Comment ctermfg=green
