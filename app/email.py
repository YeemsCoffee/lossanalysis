"""
Email sending via AWS SES using boto3.
Requires SES_FROM_EMAIL env var and the EC2 instance role to have ses:SendEmail permission.
"""

import os
import boto3
from botocore.exceptions import ClientError


def send_reset_email(to_email: str, reset_url: str, name: str = ""):
    region   = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-west-1"))
    from_email = os.environ.get("SES_FROM_EMAIL", "")

    if not from_email:
        raise RuntimeError("SES_FROM_EMAIL environment variable is not set.")

    greeting = f"Hi {name}," if name else "Hi,"

    html_body = f"""
    <div style="font-family: sans-serif; max-width: 480px; margin: 0 auto;">
      <h2 style="color: #2B4628;">Yeems Coffee — Loss Analysis</h2>
      <p>{greeting}</p>
      <p>Someone requested a password reset for your account.</p>
      <p style="margin: 24px 0;">
        <a href="{reset_url}"
           style="background:#2B4628; color:#F2ECD4; padding:12px 24px;
                  border-radius:8px; text-decoration:none; font-weight:700;">
          Reset My Password
        </a>
      </p>
      <p style="color:#6b7280; font-size:13px;">
        This link expires in <strong>1 hour</strong>.<br>
        If you didn't request this, you can safely ignore this email.
      </p>
    </div>
    """

    text_body = (
        f"{greeting}\n\n"
        f"Reset your Loss Analysis password by visiting:\n{reset_url}\n\n"
        f"This link expires in 1 hour.\n"
        f"If you didn't request this, ignore this email."
    )

    client = boto3.client("ses", region_name=region)
    client.send_email(
        Source=from_email,
        Destination={"ToAddresses": [to_email]},
        Message={
            "Subject": {"Data": "Loss Analysis — Password Reset"},
            "Body": {
                "Html": {"Data": html_body},
                "Text": {"Data": text_body},
            },
        },
    )
