-- Vault Sync — opens the vault maintenance control panel.
--
-- Deliberately holds no vault path and no folder names: it locates the
-- launcher in the papermind2026 checkout, and the launcher resolves the vault
-- itself (ChatEKLD config, then marker files). Renaming or moving the vault
-- needs no change here.
--
-- Rebuild with:
--   osacompile -o "Vault Sync.app" vault_sync.applescript
--   codesign --force --deep --sign - "Vault Sync.app"

on run
	set launcherRelPath to "/scripts/vault_maintain/launch_gui.sh"
	set homePath to (POSIX path of (path to home folder))
	-- Trailing slash from `path to home folder`; strip it for clean joins.
	if homePath ends with "/" then
		set homePath to text 1 thru -2 of homePath
	end if

	set candidates to {}
	repeat with base in {"/GitHub/papermind2026", "/papermind2026", ¬
		"/Documents/GitHub/papermind2026", "/code/papermind2026", "/dev/papermind2026"}
		set end of candidates to homePath & (base as text) & launcherRelPath
	end repeat

	set launcher to ""
	repeat with candidate in candidates
		if my fileExists(candidate as text) then
			set launcher to candidate as text
			exit repeat
		end if
	end repeat

	if launcher is "" then
		display dialog "Vault Maintenance could not start." & return & return & ¬
			"launch_gui.sh was not found in any papermind2026 checkout." & return & return & ¬
			"Looked under:" & return & homePath & "/GitHub/papermind2026" & ¬
			return & homePath & "/papermind2026" ¬
			buttons {"OK"} default button 1 with icon stop with title "Vault Maintenance"
		return
	end if

	-- Detached: the panel owns its own window and log, so this app should not
	-- sit around holding a shell open.
	do shell script "/bin/bash " & quoted form of launcher & " >/dev/null 2>&1 &"
end run

-- Pure AppleScript, no shell: coercing to `alias` throws when the file is
-- absent. (The first cut shelled out to /usr/bin/test, which does not exist on
-- macOS -- `test` is a shell builtin and the binary lives at /bin/test -- so
-- every probe raised and the launcher was reported missing even when present.)
on fileExists(posixPath)
	try
		set _probe to (POSIX file posixPath) as alias
		return true
	on error
		return false
	end try
end fileExists
