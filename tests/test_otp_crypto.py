"""What the OTP needs from pycryptodomex, on vectors made without it: oaep.py relies on its internals.

A cipher reading these differently can't talk to the server anymore, and the OTP would need a new activation.
"""
import json
import pickle
import unittest
from pathlib import Path

from Cryptodome.Cipher import AES
from Cryptodome.Hash import SHA256
from Cryptodome.PublicKey import RSA

from psa_car_controller.psa.otp import oaep
from psa_car_controller.psa.otp.otp import Otp

GOLDEN = json.loads((Path(__file__).parent / "data" / "otp_golden.json").read_text(encoding="utf-8"))
MESSAGE = bytes.fromhex("00112233445566778899aabbccddeeff")


def public_key(modulus):
    return RSA.construct((int(modulus, 16), Otp.exponent))


class TestOtpCrypto(unittest.TestCase):
    def test_exponent_of_the_vectors(self):
        self.assertEqual(17, Otp.exponent)

    def test_reads_what_the_server_sends_over_several_blocks(self):
        self.assertEqual(GOLDEN["KIW"], Otp.decode_oaep(GOLDEN["KIW_FROM_SERVER"], GOLDEN["KFACT"]))

    def test_rejects_what_another_key_sent(self):
        with self.assertRaises(ValueError):
            Otp.decode_oaep(GOLDEN["KIW_FROM_SERVER"], GOLDEN["KIW"])

    def test_rejects_a_block_of_the_wrong_length(self):
        with self.assertRaises(ValueError):
            Otp.decode_oaep(GOLDEN["KIW_FROM_SERVER"][:-2], GOLDEN["KFACT"])

    def test_encrypts_for_the_server(self):
        cipher = oaep.new(public_key(GOLDEN["KIW"]), hash_algo=SHA256, rand_func=oaep.notrandom)
        self.assertEqual(GOLDEN["TO_SERVER"], cipher.encrypt(MESSAGE).hex())

    def test_init_gets_its_cipher_from_the_server_key(self):
        otp = Otp("access-id", device_id="device")
        otp.init(Kfact=GOLDEN["KFACT"], Kiw=GOLDEN["KIW_FROM_SERVER"], pinmode="1")
        self.assertEqual(GOLDEN["KIW"], otp.Kiw)
        self.assertEqual(128, len(otp.cipher.encrypt(MESSAGE)))

    def test_saved_otp_gets_the_same_cipher_back(self):
        otp = Otp("access-id", device_id="device")
        otp.init(Kfact=GOLDEN["KFACT"], Kiw=GOLDEN["KIW_FROM_SERVER"], pinmode="1")
        loaded = pickle.loads(pickle.dumps(otp))
        self.assertNotIn(b"Cryptodome", pickle.dumps(otp))
        self.assertEqual(128, len(loaded.cipher.encrypt(MESSAGE)))
        self.assertEqual(GOLDEN["KIW"], loaded.Kiw)

    def test_aes_ecb(self):
        # FIPS 197, appendix C.1
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        encrypted = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
        self.assertEqual(encrypted, AES.new(key, AES.MODE_ECB).encrypt(MESSAGE))
        self.assertEqual(MESSAGE, AES.new(key, AES.MODE_ECB).decrypt(encrypted))
