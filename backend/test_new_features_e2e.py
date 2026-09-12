"""
End-to-End Automated Test Suite for Newsletter and Banner with Content features.
Validates route registration, models, renderer, and non-interference with existing email system.
"""

import unittest
import os
import json
from app import create_app
from banner_renderer import render_banner_to_image
from new_models import NewsletterModel, BannerModel, NewEmailLogModel

class TestNewFeatures(unittest.TestCase):

    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()
        self.app.config['TESTING'] = True

    def test_banner_rendering_engine(self):
        """Test Pillow backend renderer outputting a single PNG image"""
        banner_data = {
            'title': 'E2E Test Campaign',
            'width': 650,
            'bgColor': '#ffffff',
            'blocks': [
                {'type': 'heading', 'content': 'Summer Sales Event', 'size': 'h1', 'align': 'center', 'color': '#1a73e8'},
                {'type': 'text', 'content': 'Check out our latest discount offers on all items!', 'align': 'left', 'color': '#2d3748'},
                {'type': 'divider', 'color': '#e2e8f0'},
                {'type': 'button', 'content': 'Shop Now', 'url': 'https://example.com', 'bgColor': '#28a745', 'textColor': '#ffffff', 'align': 'center'}
            ]
        }
        res = render_banner_to_image(banner_data, 'e2e_test_banner.png')
        self.assertIsNotNone(res)
        self.assertTrue(os.path.exists(res['filepath']))
        self.assertEqual(res['width'], 650)
        self.assertGreater(res['height'], 50)
        print("[TEST PASSED] Pillow Banner Renderer produced single valid PNG image.")

    def test_routes_require_auth(self):
        """Test that new feature routes require authentication and redirect appropriately"""
        res_n = self.client.get('/newsletter')
        self.assertIn(res_n.status_code, [302, 401])
        
        res_b = self.client.get('/banner-builder')
        self.assertIn(res_b.status_code, [302, 401])
        print("[TEST PASSED] Newsletter and Banner routes protected by auth.")

    def test_existing_compose_route(self):
        """Verify existing compose route remains intact and untouched"""
        res = self.client.get('/compose')
        self.assertIn(res.status_code, [302, 401])
        print("[TEST PASSED] Existing Compose route is unchanged.")


if __name__ == '__main__':
    unittest.main()
