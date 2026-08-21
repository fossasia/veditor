# Pipeline Test Media

Do not commit or upload video or audio fixture files in this directory.

Pipeline tests generate the clips they need at run time with PyAV helpers from
`gen_video.py`, write them into pytest temporary directories, inspect them with
`helpers.py`, and let pytest clean them up. If a future edge case needs a
special clip that is awkward to produce with PyAV, commit the script that
generates it, not the binary media output.
