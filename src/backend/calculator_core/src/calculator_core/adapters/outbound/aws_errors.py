"""Safe description of AWS SDK failures for logs.

Only the exception type, the AWS error code and the operation name are reported. The error
message and the request parameters are never included: they may echo secret values.
"""

from botocore.exceptions import ClientError


def describe_error(error: BaseException) -> str:
    """Return `Type` or `Type code=<AWS code> operation=<operation>` for a ClientError."""
    name = type(error).__name__
    if isinstance(error, ClientError):
        code = str(error.response.get("Error", {}).get("Code", "Unknown"))
        return f"{name} code={code} operation={error.operation_name}"
    return name
