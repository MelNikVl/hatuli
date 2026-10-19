from terminal_extras import _photo_cache_path


def test_current_krisha_photo_cdn_can_use_the_existing_image_cache():
    assert _photo_cache_path("https://astps-photos-kr.kcdn.kz/webp/5a/image/1-750x470.jpg")
    assert _photo_cache_path("https://krisha-photos.kcdn.online/webp/5a/image/1-750x470.jpg")


def test_lookalikes_and_other_kcdn_services_are_not_implicitly_allowed():
    assert _photo_cache_path("https://astps-photos-kr.kcdn.kz.evil.example/photo.jpg") is None
    assert _photo_cache_path("https://other-service.kcdn.kz/photo.jpg") is None
    assert _photo_cache_path("https://127.0.0.1/photo.jpg") is None
