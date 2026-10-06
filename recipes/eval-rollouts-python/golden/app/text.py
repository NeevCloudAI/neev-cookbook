def slugify(title):
    """Turns a title into a URL slug.

    Lowercase ASCII letters and digits are kept (uppercase is lowercased). Every run of any other
    characters becomes a single hyphen, and the slug never starts or ends with a hyphen.
    Example: slugify("  Hello, World! 2026 ") == "hello-world-2026"
    """
    raise NotImplementedError
