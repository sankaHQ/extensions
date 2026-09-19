# SPDX-License-Identifier: Apache-2.0
import json

from openai import OpenAI

client = OpenAI()


def classify(text: str) -> str:
    try:
        response = client.responses.create(
            model="gpt-5.6-luna",
            instructions="Classify support requests as billing, technical, sales, or unknown.",
            input=text,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "support_department",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "department": {
                                "type": "string",
                                "enum": ["billing", "technical", "sales", "unknown"],
                            }
                        },
                        "required": ["department"],
                        "additionalProperties": False,
                    },
                }
            },
        )
        return json.loads(response.output_text)["department"]
    except Exception:
        return "unknown"
