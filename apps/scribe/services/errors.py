class ScribeError(Exception):
    """
    An expected pipeline failure whose message is safe to show the user.

    Anything else that escapes the pipeline is logged and shown as a generic
    error, because an arbitrary exception message can carry PHI or internals.
    """


AUDIO_MISSING = ('The recording is no longer on the server. Uploaded files are lost when '
                 'the server redeploys. Delete this encounter and upload the recording again.')
