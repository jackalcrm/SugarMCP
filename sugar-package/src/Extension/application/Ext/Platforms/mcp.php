<?php
/**
 * Registers 'mcp' as a known API platform.
 *
 * Compiles to custom/application/Ext/Platforms/platforms.ext.php on Rebuild Extensions.
 * There is no $sugar_config['platforms'] key — this is the Extension framework, and this
 * file is the supported way to add one.
 *
 * Why it matters: SugarOAuth2StorageBase::$numSessions = 1, so a password grant on an
 * existing platform runs OAuthToken::cleanupOldUserTokens() and evicts whatever session
 * already holds that slot. An integration logging in on 'base' therefore logs the user out
 * of the Sugar web UI every time it starts. A platform of its own gets its own slot.
 *
 * Separately, $sugar_config['disable_unknown_platforms'] defaults to true in 25.x, which
 * makes an unregistered platform fail the grant outright with HTTP 422
 * EXCEPTION_INVALID_PLATFORM. Registering it here is what prevents that.
 */

$platforms[] = 'mcp';
