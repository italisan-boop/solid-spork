import unittest
import uuid

from controlplane.secret_envelopes import EnvelopeCipher, SecretEnvelopeError, _read_frame


class SecretEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.cipher = EnvelopeCipher(b"k" * 32, "v1")
        self.tenant_id = str(uuid.uuid4())

    def test_envelope_is_bound_to_tenant_kind_and_generation(self):
        envelope = self.cipher.seal(
            self.tenant_id,
            "telegram_bot_token",
            1,
            "123456:tenant-token",
        )
        self.assertEqual(
            "123456:tenant-token",
            self.cipher.open(self.tenant_id, "telegram_bot_token", 1, envelope),
        )
        with self.assertRaises(SecretEnvelopeError):
            self.cipher.open(str(uuid.uuid4()), "telegram_bot_token", 1, envelope)
        with self.assertRaises(SecretEnvelopeError):
            self.cipher.open(self.tenant_id, "telegram_webhook_secret", 1, envelope)
        with self.assertRaises(SecretEnvelopeError):
            self.cipher.open(self.tenant_id, "telegram_bot_token", 2, envelope)

    def test_proxy_envelope_is_bound_to_its_tenant_kind_and_generation(self):
        envelope = self.cipher.seal(
            self.tenant_id,
            "bot_proxy_url",
            1,
            "http://proxy-user:proxy-password@203.0.113.10:3128",
        )
        self.assertEqual(
            "http://proxy-user:proxy-password@203.0.113.10:3128",
            self.cipher.open(self.tenant_id, "bot_proxy_url", 1, envelope),
        )
        with self.assertRaises(SecretEnvelopeError):
            self.cipher.open(self.tenant_id, "telegram_bot_token", 1, envelope)

    def test_delivery_envelope_is_bound_to_tenant_kind_and_generation(self):
        value = '{"version":1,"active_key_id":"delivery-v1-test","keys":{"delivery-v1-test":"test"}}'
        envelope = self.cipher.seal(
            self.tenant_id,
            "delivery_encryption_keys",
            1,
            value,
        )
        self.assertEqual(
            value,
            self.cipher.open(
                self.tenant_id, "delivery_encryption_keys", 1, envelope
            ),
        )
        with self.assertRaises(SecretEnvelopeError):
            self.cipher.open(self.tenant_id, "telegram_bot_token", 1, envelope)

    def test_fragmented_newline_frame_is_reassembled_and_trailing_data_is_rejected(self):
        class FragmentedConnection:
            def __init__(self, chunks):
                self.chunks = iter(chunks)

            def recv(self, _size):
                return next(self.chunks, b"")

        self.assertEqual(
            b'{"ok":true}',
            _read_frame(FragmentedConnection([b'{"ok"', b':true}', b'\n'])),
        )
        with self.assertRaises(SecretEnvelopeError):
            _read_frame(FragmentedConnection([b'{"ok":true}\nextra']))
        with self.assertRaises(SecretEnvelopeError):
            _read_frame(FragmentedConnection([b'{"ok":true}']))


        with self.assertRaises(SecretEnvelopeError):
            self.cipher.seal(self.tenant_id, "yookassa_credentials", 1, "secret")

    def test_rejects_lone_unicode_surrogate_before_encryption(self):
        with self.assertRaisesRegex(SecretEnvelopeError, "invalid secret value"):
            self.cipher.seal(
                self.tenant_id,
                "telegram_bot_token",
                1,
                "\ud800",
            )


if __name__ == "__main__":
    unittest.main()
