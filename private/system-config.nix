{ ... }:

{
  # System-specific configuration
  system = {
    hostname = "YOUR-HOSTNAME-HERE";
    username = "YOUR-USERNAME-HERE";
    homeDirectory = "/Users/YOUR-USERNAME-HERE";
  };
  
  # User information
  user = {
    fullName = "Your Full Name";
    email = "your.email@example.com";
    
    # Git signing key (if using Secretive or similar)
    gitSigningKey = "/path/to/your/signing/key.pub";
  };
}
