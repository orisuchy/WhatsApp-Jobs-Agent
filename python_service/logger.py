import logging

def setup_logger(name):
  logger = logging.getLogger(name)
  if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s %(message)s', datefmt='%d/%m/%y %H:%M')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
  return logger