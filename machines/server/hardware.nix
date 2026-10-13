{
  config,
  lib,
  modulesPath,
  systemConfig,
  ...
}:
{
  imports = [ (modulesPath + "/installer/scan/not-detected.nix") ];
  boot.initrd.availableKernelModules = systemConfig.hardware.initrdAvailableKernelModules;
  boot.initrd.kernelModules = systemConfig.hardware.initrdKernelModules;
  boot.kernelModules = systemConfig.hardware.kernelModules;
  boot.extraModulePackages = [ ];
  # Empty profile options mean no declaration; NixOS owns generated defaults.
  fileSystems = lib.mapAttrs (
    _: filesystem: lib.filterAttrs (name: value: name != "options" || value != [ ]) filesystem
  ) systemConfig.hardware.fileSystems;
  swapDevices = systemConfig.hardware.swapDevices;
  nixpkgs.hostPlatform = lib.mkDefault systemConfig.system.platform;
  hardware.cpu.intel.updateMicrocode = lib.mkIf (systemConfig.hardware.cpuVendor == "intel") (
    lib.mkDefault config.hardware.enableRedistributableFirmware
  );
  hardware.cpu.amd.updateMicrocode = lib.mkIf (systemConfig.hardware.cpuVendor == "amd") (
    lib.mkDefault config.hardware.enableRedistributableFirmware
  );
}
