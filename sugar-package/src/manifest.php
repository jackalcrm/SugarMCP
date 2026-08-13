<?php
/**
 * Module-loadable package for SugarMCP.
 *
 * Optional. The MCP server works without it; installing it removes the two rough edges the
 * server otherwise has to work around:
 *
 *   1. Session eviction — registers an 'mcp' API platform so the server's login does not
 *      take the session slot belonging to the user's browser.
 *   2. HTML-only endpoint discovery — adds GET /mcp/help returning JSON, in place of parsing
 *      the ~3.9 MB HTML page (which silently omits endpoints with no shortHelp).
 *
 * It also adds GET /mcp/schema/:module, which applies the field projection server-side and
 * cuts a ~299 KB metadata response to a few KB.
 */

$manifest = [
    'id' => 'sugarmcp',
    'name' => 'SugarMCP Support Package',
    'description' => 'Platform registration, OAuth key and JSON discovery endpoints for the '
        . 'SugarMCP Model Context Protocol server.',
    'version' => '0.1.0',
    'author' => 'SugarMCP',
    'is_uninstallable' => true,
    'published_date' => '2026-08-11',
    'type' => 'module',
    'acceptable_sugar_flavors' => ['PRO', 'CORP', 'ENT', 'ULT'],
    'acceptable_sugar_versions' => [
        // Sugar tests this with preg_match("/$regex/", $sugar_version) — unanchored, no
        // delimiters added beyond the slashes. A trailing $ therefore rejects dev/rc builds
        // whose version carries a suffix (e.g. 25.2.0-dev.1, 25.3.0-rc.2). Anchor the start
        // only: any 10.x–29.x major, suffix or not.
        'regex_matches' => ['^(1[0-9]|2[0-9])\\.'],
    ],
];

$installdefs = [
    'id' => 'sugarmcp',

    'copy' => [
        [
            'from' => '<basepath>/clients/base/api/McpHelpApi.php',
            'to' => 'custom/clients/base/api/McpHelpApi.php',
        ],
        [
            'from' => '<basepath>/clients/base/api/McpSchemaApi.php',
            'to' => 'custom/clients/base/api/McpSchemaApi.php',
        ],
    ],

    // The Extension framework compiles this into
    // custom/application/Ext/Platforms/platforms.ext.php on Rebuild Extensions.
    'platforms' => [
        [
            'from' => '<basepath>/Extension/application/Ext/Platforms/mcp.php',
            'to_module' => 'application',
            'name' => 'mcp',
        ],
    ],

    'post_install' => ['<basepath>/scripts/post_install.php'],
];
