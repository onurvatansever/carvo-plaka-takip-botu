import importlib.util
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Lightweight mocks so unit tests run in any CI environment without AWS credentials
if "boto3" not in sys.modules:
    boto3_mock = types.ModuleType("boto3")
    boto3_mock.client = MagicMock()
    sys.modules["boto3"] = boto3_mock

if "botocore.exceptions" not in sys.modules:
    botocore_mod = types.ModuleType("botocore")
    exceptions_mod = types.ModuleType("botocore.exceptions")

    class BotoCoreError(Exception):
        pass

    class ClientError(Exception):
        def __init__(self, error_response, operation_name):
            super().__init__(operation_name)
            self.response = error_response

    exceptions_mod.BotoCoreError = BotoCoreError
    exceptions_mod.ClientError = ClientError
    botocore_mod.exceptions = exceptions_mod
    sys.modules["botocore"] = botocore_mod
    sys.modules["botocore.exceptions"] = exceptions_mod

os.environ["TELEGRAM_TOKEN"] = "test-token"
os.environ["S3_BUCKET"] = "test-bucket"
os.environ["TELEGRAM_WEBHOOK_SECRET"] = "test-secret"
os.environ["ALLOWED_CHAT_IDS"] = "12345"

LAMBDA_PATH = Path(__file__).resolve().parent.parent / "lambda_function.py"
spec = importlib.util.spec_from_file_location("lambda_function", str(LAMBDA_PATH))
lf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lf)


def make_detection(text, det_type="LINE", conf=95.0, left=0.3, top=0.5, width=0.3, height=0.06):
    return {
        "DetectedText": text,
        "Type": det_type,
        "Confidence": conf,
        "Geometry": {
            "BoundingBox": {
                "Left": left,
                "Top": top,
                "Width": width,
                "Height": height,
            }
        },
    }


class TestPlakaBot(unittest.TestCase):
    def test_1_2_3_harfli_bosluksuz_plakalar(self):
        """Ensure 1, 2, and 3 letter plates do not eat adjacent digits (34AB1234 != 34 ABI 234)."""
        self.assertEqual(lf.plaka_coz("34A1234"), "34 A 1234")
        self.assertEqual(lf.plaka_coz("34A12345"), "34 A 12345")
        self.assertEqual(lf.plaka_coz("34AB1234"), "34 AB 1234")
        self.assertEqual(lf.plaka_coz("34AB3456"), "34 AB 3456")
        self.assertEqual(lf.plaka_coz("34AB123"), "34 AB 123")
        self.assertEqual(lf.plaka_coz("34ABC123"), "34 ABC 123")
        self.assertEqual(lf.plaka_coz("34ABC12"), "34 ABC 12")

    def test_alakasiz_kelime_ve_telefon_engeli(self):
        """Ensure background words, shop signs, and phone numbers are rejected."""
        alakasizlar = [
            "OZEL SERVIS",
            "OTO YIKAMA",
            "ISTANBUL",
            "DIZEL",
            "BILGISI",
            "0212 345 67 89",
            "0532 123 45 67",
            "0850 222 00 00",
            "WWW.OTOYIKAMA.COM.TR",
            "BMW 320I",
            "34 QWX 123",
            "A89",
            "A91",
            "NISSAN",
        ]
        for metin in alakasizlar:
            adaylar = lf.plaka_adaylari_coz(metin, sadece_34=True, eksik_34_izin_ver=False)
            self.assertEqual(adaylar, [], f"Failed to reject unrelated text: {metin} -> {adaylar}")

    def test_vida_mavi_serit_ve_plakalik_varyasyonlari(self):
        """Ensure all 23 real-world OCR variations (bolts, .TR, P80, frame phone numbers) resolve cleanly."""
        varyasyonlar = [
            "34 PBD 510",
            "TR 34 PBD 510",
            ".TR 34 PBD 510",
            "TR. 34 PBD 510",
            "TR 34. PBD 510",
            "TR 34 1 PBD 510",
            "TR 34 0 PBD 510",
            "TR 34 O PBD 510",
            "34 1 PBD 510",
            "34.PBD 510",
            "34-PBD-510",
            "TR34 PBD 510",
            "IR34 PBD 510",
            "1R34 PBD 510",
            "4 PBD 510",
            "TR 4 PBD 510",
            "34 P8D 510",
            "34 P80 510",
            "34 PBD 51O",
            "34 P8D 51O",
            "34 PBD S1O",
            "34 PBD 510 444 6 349",
            "TR 34 PBD 510 444",
        ]
        for v in varyasyonlar:
            adaylar = lf.plaka_adaylari_coz(v, sadece_34=True, eksik_34_izin_ver=True)
            self.assertTrue(adaylar, f"No candidate returned for: {v}")
            self.assertEqual(adaylar[0][0], "34 PBD 510", f"Mismatch on {v}: got {adaylar[0]}")

    def test_filo_hafizasi_bulanik_ve_kismi_eslesme(self):
        """Ensure Fleet Memory corrects OCR errors and recovers missing '34'."""
        ornek_tablo = [
            ["20.10.2026", "21.10.2026", "22.10.2026"],
            ["34 KLM 456", "34 KLM 456", "34 KLM 456"],
            ["34 AB 1234", "34 PRS 101", ""],
        ]

        # 1) Misread M->N and 6->8 on known fleet car 34 KLM 456
        lf.rekognition_client.detect_text = MagicMock(return_value={
            "TextDetections": [
                make_detection("OZEL SERVIS", "LINE", conf=99.0, height=0.02),
                make_detection("0212 555 12 34", "LINE", conf=99.0, height=0.015),
                make_detection("34 KLN 458", "LINE", conf=91.0, height=0.06),
            ]
        })
        plaka, _, detay = lf.plaka_tespit_et(b"fake", satirlar=ornek_tablo)
        self.assertEqual(plaka, "34 KLM 456")
        self.assertEqual(detay["filo_duzeltmesi"], "34 KLN 458")
        self.assertEqual(detay["alternatif"], "34 KLN 458")

        # 2) Missing '34' due to foam (only 'KLM 456' detected)
        lf.rekognition_client.detect_text = MagicMock(return_value={
            "TextDetections": [
                make_detection("KLM 456", "LINE", conf=92.0, height=0.06),
            ]
        })
        plaka2, _, _ = lf.plaka_tespit_et(b"fake", satirlar=ornek_tablo)
        self.assertEqual(plaka2, "34 KLM 456")

        # 3) Sequential fleet plate: 34 PRS 102 read cleanly with 98% confidence
        lf.rekognition_client.detect_text = MagicMock(return_value={
            "TextDetections": [
                make_detection("34 PRS 102", "LINE", conf=98.0, height=0.06),
            ]
        })
        plaka3, _, detay3 = lf.plaka_tespit_et(b"fake", satirlar=ornek_tablo)
        self.assertEqual(plaka3, "34 PRS 102")
        self.assertTrue(detay3["yeni_arac"])

        # 4) Trailing digit cut off by foam/mud ('TR 34 KLM 45' -> recovers '34 KLM 456')
        lf.rekognition_client.detect_text = MagicMock(return_value={
            "TextDetections": [
                make_detection("TR 34 KLM 45", "LINE", conf=90.0, height=0.06),
            ]
        })
        plaka4, _, _ = lf.plaka_tespit_et(b"fake", satirlar=ornek_tablo)
        self.assertEqual(plaka4, "34 KLM 456")

    def test_kelime_birlestirme_ve_kare_plaka(self):
        """Ensure WORD detections beat tiny background LINEs and square 2-line plates work."""
        ornek_tablo = [["20.10.2026"], ["34 DEF 789"]]
        lf.rekognition_client.detect_text = MagicMock(return_value={
            "TextDetections": [
                make_detection("34", "WORD", conf=96.0, left=0.35, top=0.45, width=0.08, height=0.05),
                make_detection("DEF", "WORD", conf=96.0, left=0.45, top=0.45, width=0.12, height=0.05),
                make_detection("789", "WORD", conf=95.0, left=0.40, top=0.52, width=0.12, height=0.05),
            ]
        })
        plaka, _, _ = lf.plaka_tespit_et(b"fake", satirlar=ornek_tablo)
        self.assertEqual(plaka, "34 DEF 789")

    @patch.object(lf, "telegram_mesaj_duzenle")
    @patch.object(lf, "telegram_callback_yanitla")
    @patch.object(lf, "s3_tablo_kaydet")
    @patch.object(lf, "s3_tabloyu_al")
    def test_callback_sil_ve_degistir(self, mock_al, mock_kaydet, mock_cb_yanitla, mock_duzenle):
        bugun = lf.bugunun_tarihi()
        mock_al.return_value = [[bugun], ["34 KLM 456"]]

        event_degistir = {
            "headers": {"x-telegram-bot-api-secret-token": "test-secret"},
            "body": json.dumps({
                "callback_query": {
                    "id": "cb1",
                    "from": {"id": 12345},
                    "message": {"message_id": 99, "chat": {"id": 12345}},
                    "data": "degistir:34 KLM 456:34 KLN 458",
                }
            }),
        }
        res = lf.lambda_handler(event_degistir, None)
        self.assertEqual(res["statusCode"], 200)
        self.assertEqual(mock_kaydet.call_args[0][0], [[bugun], ["34 KLN 458"]])
        mock_duzenle.assert_called_once()

        mock_al.return_value = [[bugun], ["34 KLN 458"]]
        event_sil = {
            "headers": {"x-telegram-bot-api-secret-token": "test-secret"},
            "body": json.dumps({
                "callback_query": {
                    "id": "cb2",
                    "from": {"id": 12345},
                    "message": {"message_id": 99, "chat": {"id": 12345}},
                    "data": "sil:34 KLN 458",
                }
            }),
        }
        res2 = lf.lambda_handler(event_sil, None)
        self.assertEqual(res2["statusCode"], 200)
        self.assertEqual(mock_kaydet.call_args[0][0], [[bugun]])


if __name__ == "__main__":
    unittest.main()
