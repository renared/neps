import re


def convert_scientific_notation(value, show_usage_flag=False):
    """Check if the value is a string that matches scientific e notation and convert it
    to float. (specially numbers like 3.3e-5 with a float value in front, which yaml
    can not interpret directly as float)."""

    e_notation_pattern = r"^-?\d+(\.\d+)?[eE]-?\d+$"

    flag = False  # Flag if e notation was detected

    if isinstance(value, str):
        # Remove all whitespace from the string
        value_no_space = value.replace(" ", "")

        # check for e notation
        if re.match(e_notation_pattern, value_no_space):
            flag = True

    if show_usage_flag is True:
        return float(value), flag
    else:
        return float(value)


class SearchSpaceFromYamlFileError(Exception):
    """
    Exception raised for errors occurring during the initialization of the search space
    from a YAML file.

    Attributes:
        exception_type (str): The type of the original exception.
        message (str): A detailed message that includes the type of the original exception
                       and the error description.

    Args:
        exception (Exception): The original exception that was raised during the
                                initialization of the search space from the YAML file.

    Example Usage:
        try:
            # Code to initialize search space from YAML file
        except (KeyError, TypeError, ValueError) as e:
            raise SearchSpaceFromYamlFileError(e)
    """

    def __init__(self, exception):
        self.exception_type = type(exception).__name__
        self.message = (
            f"Error occurred during initialization of search space from "
            f"YAML file.\n {self.exception_type}: {exception}"
        )
        super().__init__(self.message)


def deduce_and_validate_param_type(name, details):
    """
    Deduces the parameter type from details and validates them.

    Args:
        name (str): The name of the parameter.
        details (dict): A dictionary containing parameter specifications.

    Returns:
        str: The deduced parameter type ('int', 'float', 'categorical', or 'constant').

    Raises:
        TypeError: If the type cannot be deduced or the details don't align with expected
        constraints.
    """
    # Deduce type
    if "type" in details:
        param_type = details["type"].lower()
    else:
        # Logic to infer type if not explicitly provided
        param_type = deduce_param_type(name, details)

    # Validate details of a parameter based on (deduced) type
    validate_param_details(name, param_type, details)

    return param_type


def deduce_param_type(name, details):
    """Deduces the parameter type based on the provided details.

    This function analyzes the provided details dictionary to determine the type of
    parameter. It supports identifying integer, float, categorical, and constant
    parameter types.

    Args:
        name (str): The name of the parameter.
        details (dict): A dictionary containing parameter specifications.

    Returns:
        str: The deduced parameter type ('int', 'float', 'categorical', or 'constant').

    Raises:
        TypeError: If the parameter type cannot be deduced from the details, or if the
        provided details have inconsistent types for expected keys.

    Example:
        param_type = deduce_param_type('example_param', {'lower': 0, 'upper': 10})"""
    # Logic to deduce type from details

    # check for int and float conditions
    if "lower" in details and "upper" in details:
        # Determine if it's an integer or float range parameter
        if isinstance(details["lower"], int) and isinstance(details["upper"], int):
            param_type = "int"
        elif isinstance(details["lower"], float) and isinstance(details["upper"], float):
            param_type = "float"
        else:
            details["lower"], flag_lower = convert_scientific_notation(
                details["lower"], show_usage_flag=True
            )
            details["upper"], flag_upper = convert_scientific_notation(
                details["upper"], show_usage_flag=True
            )
            # check if one value is e notation and if so convert it to float
            if flag_lower or flag_upper:
                param_type = "float"
            else:
                raise TypeError(
                    f"Inconsistent types for 'lower' and 'upper' in '{name}'. "
                    f"Both must be either integers or floats."
                )
    # check for categorical condition
    elif "choices" in details:
        param_type = "categorical"

    # check for constant condition
    elif "value" in details:
        param_type = "constant"
    else:
        raise TypeError(
            f"Unable to deduce parameter type from {name} "
            f"with details {details}\n"
            "Supported parameters:\n"
            "Float and Integer: Expected keys: 'lower', 'upper'\n"
            "Categorical: Expected keys: 'choices'\n"
            "Constant: Expected keys: 'value'"
        )
    return param_type


def validate_param_details(name, param_type, details):
    """
    Validates the details of a parameter based on its type.

    This function checks the format and type-specific details of a parameter
    specified in a YAML file. It ensures that the 'name' of the parameter is a string
    and its 'details' are provided as a dictionary. Depending on the parameter type,
    it delegates the validation to the appropriate type-specific validation function.

    Parameters:
    name (str): The name of the parameter. It should be a string.
    param_type (str): The type of the parameter. Supported types are 'int' (or 'integer'),
                      'float', 'cat' (or 'categorical'), and 'const' (or 'constant').
    details (dict): The detailed configuration of the parameter, which includes its
                    attributes like 'lower', 'upper', 'default', etc.

    Raises:
    KeyError: If the 'name' is not a string or 'details' is not a dictionary, or if
              the necessary keys in the 'details' are missing based on the parameter type.
    TypeError: If the 'param_type' is not one of the supported types.

    Returns:
    str: The parameter type in lowercase.

    Example Usage:
    validate_param_details("learning_rate", "float", {"lower": 0.01, "upper": 0.1,
    "default": 0.05})
    """
    if not (isinstance(name, str) and isinstance(details, dict)):
        raise KeyError(
            f"Invalid format for {name} in YAML file. "
            f"Expected 'name' as string and corresponding 'details' as a "
            f"dictionary. Found 'name' type: {type(name).__name__}, 'details' "
            f"type: {type(details).__name__}."
        )
    param_type = param_type.lower()
    # init parameter by checking type
    if param_type in ("int", "integer"):
        validate_integer_parameter(name, details)

    elif param_type == "float":
        validate_float_parameter(name, details)

    elif param_type in ("cat", "categorical"):
        validate_categorical_parameter(name, details)

    elif param_type in ("const", "constant"):
        validate_constant_parameter(name, details)
    else:
        # Handle unknown parameter types
        raise TypeError(
            f"Unsupported parameter type'{details['type']}' for '{name}'.\n"
            f"Supported Types for argument type are:\n"
            "For integer parameter: int, integer\n"
            "For float parameter: float\n"
            "For categorical parameter: cat, categorical\n"
            "For constant parameter: const, constant\n"
        )
    return param_type


def validate_integer_parameter(name, details):
    """validate int parameter and convert e notation values to int"""
    # check if all keys are allowed to use and if the mandatory ones are provided
    check_keys(
        name,
        details,
        {"lower", "upper", "type", "log", "is_fidelity", "default", "default_confidence"},
        {"lower", "upper"},
    )

    if not isinstance(details["lower"], int) or not isinstance(details["upper"], int):
        try:
            # for numbers like 1e2 and 10^
            lower, flag_lower = convert_scientific_notation(
                details["lower"], show_usage_flag=True
            )
            upper, flag_upper = convert_scientific_notation(
                details["upper"], show_usage_flag=True
            )
            # check if one value format is e notation and if its an integer
            if flag_lower or flag_upper:
                if lower == int(lower) and upper == int(upper):
                    details["lower"] = int(lower)
                    details["upper"] = int(upper)
                else:
                    raise TypeError()
            else:
                raise TypeError()
        except (ValueError, TypeError) as e:
            raise TypeError(
                f"'lower' and 'upper' must be integer for " f"integer parameter '{name}'."
            ) from e
    if "default" in details:
        if not isinstance(details["default"], int):
            try:
                # convert value can raise ValueError
                default = convert_scientific_notation(details["default"])
                if default == int(default):
                    details["default"] = int(default)
                else:
                    raise TypeError()  # type of value is not int
            except (ValueError, TypeError) as e:
                raise TypeError(
                    f"default value {details['default']} "
                    f"must be integer for integer parameter {name}"
                ) from e


def validate_float_parameter(name, details):
    """validate float parameter and convert e notation values to float"""
    # check if all keys are allowed to use and if the mandatory ones are provided
    check_keys(
        name,
        details,
        {"lower", "upper", "type", "log", "is_fidelity", "default", "default_confidence"},
        {"lower", "upper"},
    )

    if not isinstance(details["lower"], float) or not isinstance(details["upper"], float):
        try:
            # for numbers like 1e-5 and 10^
            details["lower"] = convert_scientific_notation(details["lower"])
            details["upper"] = convert_scientific_notation(details["upper"])
        except ValueError as e:
            raise TypeError(
                f"'lower' and 'upper' must be integer for " f"integer parameter '{name}'."
            ) from e
    if "default" in details:
        if not isinstance(details["default"], float):
            try:
                details["default"] = convert_scientific_notation(details["default"])
            except ValueError as e:
                raise TypeError(
                    f" default'{details['default']}' must be float for float "
                    f"parameter {name} "
                ) from e


def validate_categorical_parameter(name, details):
    """validate categorical parameter and convert e notation values to float"""
    # check if all keys are allowed to use and if the mandatory ones are provided
    check_keys(
        name,
        details,
        {"choices", "type", "is_fidelity", "default", "default_confidence"},
        {"choices"},
    )

    if not isinstance(details["choices"], list):
        raise TypeError(f"The 'choices' for '{name}' must be a list.")
    for i, element in enumerate(details["choices"]):
        try:
            converted_value, e_flag = convert_scientific_notation(
                element, show_usage_flag=True
            )
            if e_flag:
                details["choices"][
                    i
                ] = converted_value  # Replace the element at the same position
        except ValueError:
            pass  # If a ValueError occurs, simply continue to the next element
    if "default" in details:
        e_flag = False
        try:
            # check if e notation, if then convert to number
            default, e_flag = convert_scientific_notation(
                details["default"], show_usage_flag=True
            )
        except ValueError:
            pass  # if default value is not in a numeric format, Value Error occurs
        if e_flag is True:
            details["default"] = default


def validate_constant_parameter(name, details):
    """Validate constant parameter and convert e notation to float"""
    # check if all keys are allowed to use and if the mandatory ones are provided
    check_keys(name, details, {"value", "type", "is_fidelity"}, {"value"})

    # check for e notation and convert it to float
    e_flag = False
    try:
        converted_value, e_flag = convert_scientific_notation(
            details["value"], show_usage_flag=True
        )
    except ValueError:
        # if the value is not able to convert to float a ValueError get raised by
        # convert_scientific_notation function
        pass
    if e_flag:
        details["value"] = converted_value


def check_keys(name, my_dict, allowed_keys, mandatory_keys):
    """
    Checks if all keys in 'my_dict' are contained in the set 'allowed_keys' and
    if all keys in 'mandatory_keys' are present in 'my_dict'.
    Raises an exception if an unallowed key is found or if a mandatory key is missing.
    """
    # Check for unallowed keys
    unallowed_keys = [key for key in my_dict if key not in allowed_keys]
    if unallowed_keys:
        unallowed_keys_str = ", ".join(unallowed_keys)
        raise KeyError(
            f"Unallowed key(s) '{unallowed_keys_str}' found for parameter '" f"{name}'."
        )

    # Check for missing mandatory keys
    missing_mandatory_keys = [key for key in mandatory_keys if key not in my_dict]
    if missing_mandatory_keys:
        missing_keys_str = ", ".join(missing_mandatory_keys)
        raise KeyError(
            f"Missing mandatory key(s) '{missing_keys_str}' for parameter '" f"{name}'."
        )
