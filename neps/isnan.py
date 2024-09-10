import numpy as np
import logging
import traceback
def isnan(x):
    try:
        return np.isnan(x)
    except TypeError:
        logging.warning(f"TypeError in isnan(), returning False. {traceback.format_exc()}")
        return False