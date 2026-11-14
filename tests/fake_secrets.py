"""Runtime-built credential-shaped values for redaction tests and demos."""


def stripe_live_key(suffix="FAKEKEY1234567890"):
    return "sk" + "-live-" + suffix


def github_token(prefix="p", suffix="abcdefghijklmnopqrstuvwxyz123456789012345678"):
    return "gh" + prefix + "_" + suffix


def github_app_credential(suffix="abcdefghijklmnopqrstuvwxyz0123456789"):
    return "github" + "_pat_" + suffix


def aws_access_key(suffix="1234567890ABCDEF"):
    return "AK" + "IA" + suffix


def slack_token(prefix="b", first="1234567890", second="abcdefghijklmnop"):
    return "xo" + "x" + prefix + "-" + first + "-" + second


def bearer_token():
    return "Bearer " + "long." + "example-" + "token_value"


def private_key_block():
    header = "-" * 5 + "BEGIN " + "PRIVATE KEY" + "-" * 5
    footer = "-" * 5 + "END " + "PRIVATE KEY" + "-" * 5
    return header + "\nprivate-material\n" + footer
