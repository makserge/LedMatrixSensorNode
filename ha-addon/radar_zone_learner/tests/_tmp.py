import os, tempfile
def tmpdir(prefix="rzl-"):
    base = os.environ.get("RZL_TEST_TMP")
    if base:
        os.makedirs(base, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix=prefix, dir=base or None)
