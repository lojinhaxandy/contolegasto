# Shim de imghdr para Python 3.13+
# Implementa apenas imghdr.what() suficiente para python-telegram-bot.
# Requer Pillow no requirements.

from io import BytesIO

def what(file, h=None):
    try:
        from PIL import Image
        if h is None:
            # file pode ser path ou file-like
            if hasattr(file, "read"):
                img = Image.open(file)
            else:
                with open(file, "rb") as f:
                    img = Image.open(f)
            img.verify()  # valida header sem ler tudo
            return (img.format or "").lower()  # "jpeg", "png", etc.
        else:
            img = Image.open(BytesIO(h))
            img.verify()
            return (img.format or "").lower()
    except Exception:
        return None
