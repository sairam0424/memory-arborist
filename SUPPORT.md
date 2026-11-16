# Support

memory-arborist is a small beta maintained by one person, so support is best effort.

- **Something is broken or looks wrong:** run `python3 -I <plugin folder>/engine/doctor/memory_doctor.py` from the project whose memory is affected (see the README, "Your first run", for the plugin folder) and open an issue with its output. It holds names, versions and counts only. Please do not paste notes or transcript text.
- **A question about how it works:** read the README ("How safe is it, exactly?", "When something goes wrong") and `docs/PROVENANCE.md` first, then open an issue.
- **A security problem:** do not open an issue; see `SECURITY.md`.
- **Your login route is not Bedrock:** it is untested. `doctor/memory_doctor.py --live` makes one cheap real call and tells you whether it works; a report of the result (with the doctor output) is very welcome.

Only the latest release receives fixes while the project is pre-1.0.
