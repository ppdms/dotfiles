import unittest

from credential_validation import parse_payload, payload_bytes, validate_fields


class RegistryCredentialTests(unittest.TestCase):
    def test_header_injection_or_non_ascii_token_is_rejected_without_secret_echo(self):
        for token in (
            "ghp_dummy\r\nX-Evil: injected", "ghp_dummy\n", "ghp_dummy with-space",
            "ghp_dummy\t", "ghp_dummy\x00", "ghp_dummy\x01", "ghp_dummyé",
        ):
            with self.subTest(token_type=repr(token[-1:])):
                with self.assertRaises(ValueError) as error:
                    validate_fields("image-updates", {"github-token": token})
                self.assertNotIn(token, str(error.exception))
                self.assertNotIn("ghp_dummy", str(error.exception))

    def test_missing_or_empty_token_cannot_be_saved_as_complete_credentials(self):
        for fields in ({}, {"github-token": ""}):
            with self.subTest(fields_present=bool(fields)), self.assertRaises(ValueError):
                parse_payload("image-updates", payload_bytes("image-updates", fields))


if __name__ == "__main__":
    unittest.main()
