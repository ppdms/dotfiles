if (match(system("hostname -s"), 'Macintosh') >= 0)
        call plug#begin()
                "Plug 'SirVer/ultisnips'
                Plug 'honza/vim-snippets'
                Plug 'tibabit/vim-templates'
                Plug 'vim-autoformat/vim-autoformat'
        call plug#end()
endif

let g:tmpl_author_name = 'Vasileios Papadimas'
let g:tmpl_author_email = 'papadimas@protonmail.com'
let g:tmpl_search_paths = ['/Users/basil/sandbox/dotfiles/templates']
let g:formatdef_latexindent = '"latexindent -"'

set background=dark
"some of the following forked from geohot
set nocompatible
filetype plugin indent on
syntax on
set tabstop=4
set shiftwidth=4
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
set mouse+=a
set termguicolors
vmap <C-C> "+y
noremap <F3> :Autoformat<CR>
