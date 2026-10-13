# Photos importer (Mac only)

A small application that imports an image into Photos under add-only
permission: it adds photos and can neither read nor delete the
library. Source and packaging live in `programs/photos-import/`. Home
Manager installs it among the other Mac applications, and Hammerspoon
invokes it for screenshot shortcuts; that wiring sits in
`machines/macbook/home.nix`.

A standalone build, with no change to the machine:

```bash
bash programs/photos-import/build.sh
```

The command prints the immutable application and CLI paths and
creates nothing in the checkout. Verification of a fresh build:

```bash
codesign --verify --deep --strict "<printed app path>"
"<printed CLI path>" /path/to/a/test-image.png
```
