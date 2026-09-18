def test_package_importable():
    import football_lottery
    assert football_lottery.__name__ == "football_lottery"
