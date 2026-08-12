<?php
/**
 * Post-install: create the OAuth consumer key the MCP server authenticates with.
 *
 * The key must have client_type = 'user'. OAuth2Api::isClientAllowed() accepts a key only
 * when its client_type is 'user' or matches the platform store's clientType, and
 * SugarOAuth2StorageBase — which is what any custom platform gets — has clientType = null.
 * A key created with the default type therefore fails on the 'mcp' platform with
 * invalid_client, which is a confusing failure to diagnose after the fact.
 *
 * The generated secret is printed once, during install. It is not recoverable afterwards
 * (Sugar stores it hashed for comparison), so it must be copied into the server's .env now.
 */

function post_install()
{
    $key = 'mcp';

    $existing = BeanFactory::newBean('OAuthKeys');
    $found = $existing->retrieve_by_string_fields([
        'c_key' => $key,
        'oauth_type' => 'oauth2',
        'deleted' => 0,
    ]);

    if (!empty($found->id)) {
        // Re-running install must not silently rotate a secret the server is already using.
        echo '<h3>SugarMCP: OAuth key already present</h3>';
        echo "<p>An OAuth2 consumer key named <code>{$key}</code> already exists, so it was "
            . 'left untouched. If you no longer have its secret, delete the key in '
            . 'Admin &rarr; OAuth Keys and reinstall this package.</p>';

        if ($found->client_type !== 'user') {
            $found->client_type = 'user';
            $found->save();
            echo '<p><strong>Corrected</strong> its client_type to <code>user</code>, which '
                . 'is required for a custom platform.</p>';
        }
        return;
    }

    $secret = bin2hex(random_bytes(24));

    $oauthKey = BeanFactory::newBean('OAuthKeys');
    $oauthKey->name = 'SugarMCP';
    $oauthKey->c_key = $key;
    $oauthKey->c_secret = $secret;
    $oauthKey->oauth_type = 'oauth2';
    $oauthKey->client_type = 'user';
    $oauthKey->description = 'Consumer key for the SugarMCP server (Model Context Protocol).';
    $oauthKey->save();

    echo '<h3>SugarMCP installed</h3>';
    echo '<p>Copy these into the MCP server&rsquo;s <code>.env</code> file. '
        . '<strong>The secret is shown only once.</strong></p>';
    echo '<pre style="padding:12px;background:#f4f4f4;border:1px solid #ccc">'
        . "SUGAR_PLATFORM=mcp\n"
        . "SUGAR_CLIENT_ID={$key}\n"
        . "SUGAR_CLIENT_SECRET={$secret}\n"
        . '</pre>';
    echo '<p>Then run <em>Admin &rarr; Repair &rarr; Quick Repair and Rebuild</em> so the '
        . 'new platform and API endpoints are registered.</p>';
}
