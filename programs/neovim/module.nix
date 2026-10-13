{
  lib,
  pkgs,
  systemConfig,
  ...
}:

# Laptop Neovim settings with store-managed plugins, parsers, and templates.
let
  vim-template = pkgs.vimUtils.buildVimPlugin {
    pname = "vim-template";
    version = "2026-03-16";
    src = pkgs.fetchFromGitHub {
      owner = "aperezdc";
      repo = "vim-template";
      rev = "9d62a97df1ededc838b9f6ff62751f63c8a22073";
      hash = "sha256-MUpA8OPkaf28SgZ6Idczb1M0kZowcOzs+IVDcEaK09A=";
    };
    meta.homepage = "https://github.com/aperezdc/vim-template";
  };

  treesitterWithGrammars = pkgs.vimPlugins.nvim-treesitter.withPlugins (p: [
    # Template languages.
    p.c
    p.cmake
    p.cpp
    p.html
    p.java
    p.javascript
    p.lua
    p.make
    p.python
    p.tsx
    p.typescript
    p.vim
    p.vimdoc
    # Current repo languages and common companions.
    p.bash
    p.css
    p.comment
    p.diff
    p.dockerfile
    p.fish
    p.gitcommit
    p.gitignore
    p.go
    p.ini
    p.jsdoc
    p.json
    p.markdown
    p.markdown_inline
    p.nix
    p.query
    p.regex
    p.rust
    p.toml
    p.yaml
  ]);

in
{
  xdg.configFile."nvim/templates".source = ./templates;

  programs.neovim = {
    enable = true;
    viAlias = true;
    vimAlias = true;
    defaultEditor = true;
    withRuby = false;
    # vim-autoformat shells formatters out through embedded Python; without the
    # provider F3 degrades to indent/retab/whitespace cleanup only.
    withPython3 = true;

    initLua = ''
      -- Basic settings
      vim.opt.number = true
      vim.opt.expandtab = true
      vim.opt.tabstop = 4
      vim.opt.shiftwidth = 4
      vim.opt.mouse = 'a'
      vim.opt.termguicolors = true
    '';

    extraPackages = with pkgs; [
      # First-choice formatter per filetype below; the wrapped nvim PATH is
      # what vim-autoformat shells out to.
      ruff
      shfmt
      nixfmt
      astyle
      clang-tools
      prettier
      jq
      html-tidy
      stylua
    ];

    plugins = with pkgs.vimPlugins; [
      vim-snippets
      {
        plugin = vim-template;
        type = "viml";
        config = ''
          let g:username = '${lib.replaceStrings [ "'" ] [ "''" ] systemConfig.user.fullName}'
          let g:email = '${lib.replaceStrings [ "'" ] [ "''" ] systemConfig.user.templateEmail}'
          let g:templates_directory = [stdpath('config') . '/templates']
          let g:templates_no_builtin_templates = 1
          let g:templates_use_licensee = 0
        '';
      }
      {
        plugin = vim-autoformat;
        type = "viml";
        config = ''
          " First-choice formatter per filetype; every binary ships in
          " programs.neovim.extraPackages so F3 never silently degrades to
          " indent/retab/whitespace cleanup for these filetypes.
          let g:formatters_python = ['ruff']
          let g:formatters_sh = ['shfmt']
          let g:formatters_nix = ['nix_format']
          let g:formatters_c = ['clangformat']
          let g:formatters_cpp = ['clangformat']
          let g:formatters_java = ['astyle_java']
          let g:formatters_javascript = ['prettier']
          let g:formatters_javascriptreact = ['prettier']
          let g:formatters_typescript = ['prettier']
          let g:formatters_typescriptreact = ['prettier']
          let g:formatters_json = ['jq']
          let g:formatters_html = ['tidy_html']
          let g:formatters_xml = ['tidy_xml']
          let g:formatters_xhtml = ['tidy_xhtml']
          let g:formatters_lua = ['stylua']
          let g:formatters_css = ['prettier']
          let g:formatters_markdown = ['prettier']
          let g:formatters_yaml = ['prettier']
          " latexindent comes from the shared document environment.
          let g:formatdef_latexindent = '"latexindent -"'
          let g:formatters_tex = ['latexindent']
          let g:formatters_plaintex = ['latexindent']
        '';
      }
      {
        plugin = treesitterWithGrammars;
        type = "lua";
        config = ''
          -- Parsers/queries ship from the Nix store; never fetch at runtime.
          require('nvim-treesitter').setup({})
          vim.api.nvim_create_autocmd('FileType', {
            callback = function(event)
              local language = vim.treesitter.language.get_lang(event.match)
              if not language or #vim.api.nvim_get_runtime_file('parser/' .. language .. '.*', false) == 0 then
                return
              end
              vim.treesitter.start(event.buf, language)
              vim.wo[0][0].foldexpr = 'v:lua.vim.treesitter.foldexpr()'
              vim.wo[0][0].foldmethod = 'expr'
              vim.bo.indentexpr = "v:lua.require'nvim-treesitter'.indentexpr()"
            end,
          })
        '';
      }
    ];

    extraConfig = ''
      " Key mappings
      vmap <C-C> "+y
      noremap <F3> :Autoformat<CR>
    '';
  };
}
