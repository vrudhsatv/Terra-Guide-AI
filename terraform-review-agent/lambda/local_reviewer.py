import json
import os
import urllib.request
import urllib.error


# ============================================================
# Configuration
# ============================================================

GEMINI_MODEL = "gemini-3.5-flash"

# Gemini 3.5 Flash supports:
# minimal, low, medium, high
#
# medium is the recommended starting point for most tasks.
GEMINI_THINKING_LEVEL = "medium"

GEMINI_API_KEY = None


# ============================================================
# Gemini API Key
# ============================================================

def get_gemini_api_key():
    """
    Get Gemini API key from the local environment.

    For local development we intentionally do NOT use:
        - AWS Secrets Manager
        - boto3
        - AWS credentials

    Set the key before running:

        export GEMINI_API_KEY="your-api-key"
    """

    global GEMINI_API_KEY

    if GEMINI_API_KEY:
        return GEMINI_API_KEY

    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY environment variable is not set.\n"
            "Run:\n"
            "export GEMINI_API_KEY='your-api-key'"
        )

    return GEMINI_API_KEY


# ============================================================
# Terrascan Processing
# ============================================================

def extract_relevant_findings(terrascan_results: dict) -> dict:
    """
    Extract only the information required by the AI reviewer
    from Terrascan output.
    """

    violations = terrascan_results.get("violations", [])
    summary = terrascan_results.get("scan_summary", {})

    structured = {
        "summary": {
            "total_violations": summary.get(
                "violated_policies",
                0
            ),
            "high": summary.get("high", 0),
            "medium": summary.get("medium", 0),
            "low": summary.get("low", 0)
        },
        "violations": []
    }

    for violation in violations:

        structured["violations"].append({
            "rule_id": violation.get("rule_id"),
            "rule_name": violation.get("rule_name"),
            "severity": violation.get("severity"),
            "description": violation.get("description"),
            "resource_type": violation.get("resource_type"),
            "resource_name": violation.get("resource_name"),
            "file": violation.get("file"),
            "line": violation.get("line")
        })

    return structured


# ============================================================
# AI Prompt
# ============================================================

def build_prompt(findings: dict) -> str:
    """
    Build the Terraform security-review prompt.

    The decision policy is intentionally explicit so that
    the AI acts as a security gate rather than simply giving
    a general Terraform review.
    """

    return f"""
You are a senior DevOps and Terraform security reviewer
acting as a CI/CD security gate.

Your task is to analyze Terrascan findings and determine
whether the Terraform infrastructure should be allowed
to proceed.

IMPORTANT:

This is a CI/CD security decision.

You MUST follow the decision policy below.

============================================================
DECISION POLICY
============================================================

REJECT if ANY of the following are true:

1. Any HIGH severity issue exists.

2. Any CRITICAL severity issue exists.

3. MEDIUM severity issues are greater than or equal to 4.

4. The Terraform configuration contains an Application
   Load Balancer but has no HTTPS listener at all.

------------------------------------------------------------

APPROVE_WITH_CHANGES if:

1. MEDIUM severity issues are between 1 and 3 inclusive.

AND:

2. There are no HIGH or CRITICAL issues.

AND:

3. The ALB HTTPS requirement is satisfied.

------------------------------------------------------------

APPROVE if:

1. There are only LOW or INFO issues.

AND:

2. There are no MEDIUM, HIGH, or CRITICAL issues.

AND:

3. The ALB HTTPS requirement is satisfied.

============================================================
OUTPUT FORMAT
============================================================

Provide exactly these sections:

1. 🚨 Security Issues

List the important security issues ordered by severity.

2. 🛠 Required Remediation

List only actionable remediation items.

3. ⚖️ Risk Justification

Give a concise 1–2 line explanation for the decision.

4. 📌 Final Verdict

The final verdict MUST be exactly one of:

APPROVE
APPROVE_WITH_CHANGES
REJECT

============================================================
RULES
============================================================

- Be concise.
- Use bullet points.
- Focus on AWS infrastructure.
- Pay particular attention to:
  - ALB
  - ECS
  - VPC
  - IAM
  - Security Groups
  - HTTPS/TLS
  - Public exposure
  - Encryption
  - Least privilege
- Ignore Terrascan scan_errors.
- Do not repeat the raw JSON.
- Do not invent findings that are not present.
- Do not override the decision policy.
- The final verdict must strictly follow the policy.
- If the evidence is insufficient to safely approve the
  infrastructure, choose REJECT.
- Never silently approve an unclear result.

============================================================
TERRASCAN FINDINGS
============================================================

{json.dumps(findings, indent=2)}
"""


# ============================================================
# Gemini API
# ============================================================

def call_gemini(prompt: str) -> str:
    """
    Call Gemini 3.5 Flash using the REST GenerateContent API.

    No AWS dependency is required.
    """

    api_key = get_gemini_api_key()

    url = (
        "https://generativelanguage.googleapis.com/"
        "v1beta/models/"
        f"{GEMINI_MODEL}:generateContent"
    )

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": prompt
                    }
                ]
            }
        ],
        "generationConfig": {
            "thinkingConfig": {
                "thinkingLevel": GEMINI_THINKING_LEVEL
            }
        }
    }

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key
        },
        method="POST"
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=60
        ) as response:

            response_body = response.read()

            result = json.loads(response_body)

            # Expected response:
            #
            # candidates
            #   └── content
            #        └── parts
            #             └── text

            candidates = result.get("candidates", [])

            if not candidates:
                return (
                    "Unexpected Gemini API response: "
                    "no candidates returned.\n"
                    f"{json.dumps(result, indent=2)}"
                )

            content = candidates[0].get("content", {})

            parts = content.get("parts", [])

            text_parts = []

            for part in parts:

                # Gemini may return parts that aren't normal text.
                if part.get("text"):
                    text_parts.append(part["text"])

            if not text_parts:
                return (
                    "Unexpected Gemini API response: "
                    "no text returned.\n"
                    f"{json.dumps(result, indent=2)}"
                )

            return "\n".join(text_parts).strip()

    except urllib.error.HTTPError as error:

        error_body = error.read().decode(
            "utf-8",
            errors="replace"
        )

        return (
            f"Gemini API HTTP error ({error.code}):\n"
            f"{error_body}"
        )

    except urllib.error.URLError as error:

        return (
            "Gemini API connection error:\n"
            f"{error}"
        )

    except TimeoutError:

        return (
            "Gemini API request timed out."
        )

    except json.JSONDecodeError as error:

        return (
            "Failed to decode Gemini API response:\n"
            f"{error}"
        )

    except Exception as error:

        return (
            "Unexpected error calling Gemini:\n"
            f"{error}"
        )


# ============================================================
# Verdict Extraction
# ============================================================

def extract_verdict(review_text: str) -> str:
    """
    Extract the final verdict from Gemini's response.

    Fail-safe behavior:
    If the verdict cannot be confidently identified,
    return REJECT.
    """

    if not review_text:
        return "REJECT"

    text = review_text.upper()

    # We only want to inspect the area around FINAL VERDICT
    # rather than searching the entire AI response.
    #
    # This prevents a word such as "REJECT" appearing in a
    # remediation explanation from accidentally becoming
    # the final decision.

    marker = "FINAL VERDICT"

    if marker not in text:
        return "REJECT"

    verdict_section = text.split(
        marker,
        1
    )[1]

    # Check the more specific value first.
    #
    # APPROVE_WITH_CHANGES contains the word APPROVE.
    if "APPROVE_WITH_CHANGES" in verdict_section:
        return "APPROVE_WITH_CHANGES"

    if "REJECT" in verdict_section:
        return "REJECT"

    if "APPROVE" in verdict_section:
        return "APPROVE"

    # Fail closed.
    return "REJECT"


# ============================================================
# Lambda-Compatible Handler
# ============================================================

def lambda_handler(event, context=None):
    """
    Local-compatible Lambda handler.

    Expected event:

    {
        "results": {
            "scan_summary": {
                "violated_policies": 1,
                "high": 0,
                "medium": 1,
                "low": 0
            },
            "violations": [
                ...
            ]
        }
    }
    """

    try:

        # ----------------------------------------------------
        # Validate event
        # ----------------------------------------------------

        if not isinstance(event, dict):

            return {
                "statusCode": 400,
                "verdict": "REJECT",
                "error": "Event must be a JSON object"
            }

        results = event.get("results")

        if not results:

            return {
                "statusCode": 400,
                "verdict": "REJECT",
                "error": "Missing Terrascan results in payload"
            }

        # ----------------------------------------------------
        # Extract findings
        # ----------------------------------------------------

        findings = extract_relevant_findings(
            results
        )

        # ----------------------------------------------------
        # Build AI prompt
        # ----------------------------------------------------

        prompt = build_prompt(
            findings
        )

        # ----------------------------------------------------
        # Call Gemini
        # ----------------------------------------------------

        ai_review = call_gemini(
            prompt
        )

        # ----------------------------------------------------
        # Check for Gemini errors
        # ----------------------------------------------------

        gemini_error_prefixes = (
            "Gemini API HTTP error",
            "Gemini API connection error",
            "Unexpected Gemini API response",
            "Failed to decode Gemini API response",
            "Unexpected error calling Gemini",
            "Gemini API request timed out"
        )

        if ai_review.startswith(
            gemini_error_prefixes
        ):

            return {
                "statusCode": 502,
                "verdict": "REJECT",
                "summary": findings["summary"],
                "error": ai_review
            }

        # ----------------------------------------------------
        # Extract verdict
        # ----------------------------------------------------

        verdict = extract_verdict(
            ai_review
        )

        # ----------------------------------------------------
        # Return result
        # ----------------------------------------------------

        return {
            "statusCode": 200,
            "verdict": verdict,
            "summary": findings["summary"],
            "ai_review": ai_review
        }

    except Exception as error:

        # Fail closed.
        return {
            "statusCode": 500,
            "verdict": "REJECT",
            "error": str(error)
        }


# ============================================================
# Local CLI Test
# ============================================================

if __name__ == "__main__":

    print("=" * 70)
    print("Local AI Terraform Reviewer")
    print("=" * 70)

    print(f"Model          : {GEMINI_MODEL}")
    print(f"Thinking level : {GEMINI_THINKING_LEVEL}")
    print()

    # Simple local test payload.
    #
    # This is NOT real Terrascan output.
    # It is only used to verify that:
    #
    # Terrascan-like JSON
    #       ↓
    # Lambda handler
    #       ↓
    # Gemini
    #       ↓
    # Verdict
    #
    # works correctly.

    test_event = {
        "results": {
            "scan_summary": {
                "violated_policies": 1,
                "high": 1,
                "medium": 0,
                "low": 0
            },
            "violations": [
                {
                    "rule_id": "AC_AWS_001",
                    "rule_name": "Example High Severity Rule",
                    "severity": "HIGH",
                    "description": (
                        "Example high severity "
                        "Terraform security issue."
                    ),
                    "resource_type": "aws_security_group",
                    "resource_name": "example",
                    "file": "main.tf",
                    "line": 10
                }
            ]
        }
    }

    result = lambda_handler(
        test_event,
        None
    )

    print(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False
        )
    )
