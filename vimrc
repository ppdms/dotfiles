if (match(system("hostname -s"), 'Macintosh') >= 0)
        call plug#begin()
        Plug 'ActivityWatch/aw-watcher-vim'
        call plug#end()
endif
set background=dark
"some of the following forked from geohot
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
set shortmess+=I
set wrap
set linebreak
set formatoptions=ro
set comments=b:-
set breakindentopt=shift:4
set breakindent
set autoindent
