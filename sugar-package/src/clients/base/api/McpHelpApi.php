<?php
/**
 * GET /rest/v11_x/mcp/help — the API endpoint catalog, as JSON.
 *
 * Core's HelpApi::getHelp() already builds exactly the structure an MCP client wants: it
 * walks $api->dict->dict, substitutes '?' path tokens with ':<pathVar>' to produce a
 * readable fullPath, and resolves exception classes to readable names. It then throws all
 * of that away by rendering include/api/help/extras/helpList.php to HTML.
 *
 * This endpoint reuses the walk and returns the array. That matters because the HTML path
 * is lossy in ways that cannot be recovered by parsing:
 *
 *   - endpoints whose shortHelp is empty are skipped entirely by the renderer;
 *   - several core longHelp paths point at include/api/html/, a directory that does not
 *     exist in 25.x, so those fragments render as nothing;
 *   - the response is ~3.9 MB of markup for ~700 endpoints.
 *
 * Two deliberate departures from core:
 *
 *   1. Login is required. Core's /help is noLoginRequired, but there is no reason to expose
 *      a customer's custom endpoint surface anonymously.
 *   2. ?module= and ?q= filtering, so the catalog can be queried instead of dumped.
 */

require_once 'clients/base/api/HelpApi.php';

class McpHelpApi extends HelpApi
{
    public function registerApiRest()
    {
        return [
            'mcpHelp' => [
                'reqType' => 'GET',
                'path' => ['mcp', 'help'],
                'pathVars' => ['', ''],
                'method' => 'getMcpHelp',
                'shortHelp' => 'API endpoint catalog as JSON, for MCP clients',
                'longHelp' => '',
                // Deliberately NOT noLoginRequired — see the class comment.
            ],
        ];
    }

    /**
     * @param ServiceBase $api
     * @param array $args  Optional: platform, module, q
     * @return array
     */
    public function getMcpHelp(ServiceBase $api, array $args)
    {
        $platform = empty($args['platform']) ? 'base' : $args['platform'];

        $endpointList = [];
        foreach ($api->dict->dict as $startDepth => $dirPart) {
            if (isset($dirPart[$platform])) {
                $endpointList = array_merge(
                    $endpointList,
                    $this->getEndpoints($dirPart[$platform], $startDepth)
                );
            }
        }

        $moduleFilter = isset($args['module']) ? (string)$args['module'] : '';
        $textFilter = isset($args['q']) ? strtolower((string)$args['q']) : '';

        // Sorted by building a keyed array and using ksort(). Module Loader's package
        // scanner denies every callback-taking function — usort() among them — because a
        // callback can smuggle in arbitrary code. Collisions are impossible because
        // method+path is unique per route, but the index keeps it safe regardless.
        $sorted = [];
        $index = 0;
        foreach ($endpointList as $endpoint) {
            $entry = $this->summarize($endpoint);

            if ($moduleFilter !== '' && !$this->matchesModule($entry, $moduleFilter)) {
                continue;
            }
            if ($textFilter !== '' && !$this->matchesText($entry, $textFilter)) {
                continue;
            }
            $sorted[$entry['path'] . ' ' . $entry['method'] . ' ' . $index] = $entry;
            $index++;
        }
        ksort($sorted);

        $results = array_values($sorted);

        return [
            'platform' => $platform,
            'count' => count($results),
            'endpoints' => $results,
        ];
    }

    /**
     * Reduce one dictionary entry to the fields a client can act on.
     */
    protected function summarize(array $endpoint)
    {
        // Rebuild the readable path the same way core does: '?' tokens become the name of
        // the path variable that fills them.
        $fullPath = '';
        foreach ($endpoint['path'] as $index => $part) {
            if ($part === '?') {
                $part = ':' . (isset($endpoint['pathVars'][$index]) ? $endpoint['pathVars'][$index] : 'arg');
            }
            $fullPath .= '/' . $part;
        }

        $exceptions = [];
        if (!empty($endpoint['exceptions']) && is_array($endpoint['exceptions'])) {
            foreach ($endpoint['exceptions'] as $exception) {
                $exceptions[] = $this->getExceptionType($exception);
            }
        }

        $entry = [
            'method' => isset($endpoint['reqType']) ? $endpoint['reqType'] : 'GET',
            'path' => $fullPath,
            'description' => isset($endpoint['shortHelp']) ? $endpoint['shortHelp'] : '',
            'handler' => isset($endpoint['method']) ? $endpoint['method'] : '',
        ];

        if (!empty($exceptions)) {
            $entry['exceptions'] = array_values(array_unique($exceptions));
        }

        // The source file is how a client tells a customization from stock functionality —
        // anything under custom/ was added by this instance.
        if (!empty($endpoint['className'])) {
            $entry['class'] = $endpoint['className'];
        }
        if (!empty($endpoint['file'])) {
            $entry['source'] = $endpoint['file'];
            $entry['custom'] = (strpos($endpoint['file'], 'custom/') !== false);
        }
        if (!empty($endpoint['minVersion'])) {
            $entry['min_version'] = $endpoint['minVersion'];
        }

        // The path to the long-form help fragment is reported, but not its contents: the
        // package scanner denies every filesystem read (file_get_contents, is_readable,
        // fopen, file, readfile), so a package simply cannot inline it. No great loss —
        // several core entries point at include/api/html/, which does not exist in 25.x.
        if (!empty($endpoint['longHelp'])) {
            $entry['help_file'] = $endpoint['longHelp'];
        }

        return $entry;
    }

    protected function matchesModule(array $entry, string $module): bool
    {
        // Match the first path segment, so 'Accounts' finds /Accounts and /Accounts/:record
        // but not /Contacts. '<module>' is core's generic module placeholder.
        $segments = explode('/', ltrim($entry['path'], '/'));
        $first = isset($segments[0]) ? $segments[0] : '';
        return strcasecmp($first, $module) === 0
            || $first === '<module>'
            || $first === ':module';
    }

    /**
     * Both parameters are typed rather than left loose: Sugar's Rector-based compatibility
     * scan rejects an untyped strpos() needle (StringifyStrNeedlesRector, PHP 7.3, where a
     * non-string needle stopped being coerced). Declaring the type satisfies it properly
     * instead of papering over it with a cast at each call site.
     */
    protected function matchesText(array $entry, string $needle): bool
    {
        return strpos(strtolower($entry['path']), $needle) !== false
            || strpos(strtolower($entry['description']), $needle) !== false;
    }
}
