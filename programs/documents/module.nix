{ pkgs, ... }:
let
  documentTex = pkgs.texlive.combine {
    inherit (pkgs.texlive)
      scheme-basic
      collection-latexrecommended
      babel-english
      ec
      cm-super
      babel-greek
      cbfonts
      greek-fontenc
      greek-inputenc
      hyphen-greek
      hyphen-ancientgreek
      enumitem
      latexmk
      latexindent
      ;
  };
in
{
  home.packages = with pkgs; [
    pandoc
    documentTex
    poppler-utils
    tesseract
    ffmpeg
  ];
  programs.neovim.extraPackages = [ documentTex ];
}
