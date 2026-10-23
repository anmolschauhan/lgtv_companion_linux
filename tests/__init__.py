import logging

# Keep expected warnings (e.g. "did not power on") out of the test output.
logging.getLogger("lgtv_companion").addHandler(logging.NullHandler())
