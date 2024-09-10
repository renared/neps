import numpy as np
import logging
import traceback
def isnan(x):
    try:
        return np.isnan(x)
    except TypeError:
        logging.warning(f"TypeError in isnan(), returning True array. {traceback.format_exc()}")
        return np.full_like(x, fill_value=True)