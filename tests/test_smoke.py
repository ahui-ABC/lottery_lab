def test_package_importable():
    import lottery_lab
    assert lottery_lab.__name__ == "lottery_lab"
