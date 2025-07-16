# Test configuration for rate limiting testing
SERVERTAG = "test"
HOST, PORT = "irc.oftc.net", 6667  # Plain text, no SSL

# Test bot settings
CHANNEL = "#beholder_test"
NICK = "beholder_test"
USERNAME = "beholder_test"
REALNAME = "Rate Limiting Test Bot"

# Test environment settings
BOTDIR = "/tmp"
PWFILE = "/tmp/empty_pw"  # Empty password file for test
TEST = True  # Skip file existence checks
DISABLE_SASL = True  # Disable SASL for testing

# Minimal required paths (not used in test)
FILEROOT = "/tmp/"
WEBROOT = "https://example.com/"
LOGROOT = "/tmp/"
PINOBOT = "nonexistent"
DCBRIDGE = "nonexistent"

# Admin for testing - add your IRC nick here
ADMIN = ["build"]

# No remote servers for testing
SLAVE = False

# Minimal variants dict to prevent errors
VARIANTS = {
    "test": ("Test Variant", "test")
}