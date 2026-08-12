<?php
/**
 * GET /rest/v11_x/mcp/schema/:module — a pruned field projection, computed server-side.
 *
 * GET /metadata?type_filter=modules&module_filter=X returns ~299 KB for a single module on a
 * customized instance, of which the client keeps about 3%: views (158 KB), layouts, filters
 * and dependencies are all client-rendering data that an API consumer never uses.
 *
 * The MCP server already prunes this on its side, so this endpoint is an optimisation rather
 * than a capability — it moves the pruning to where the data already is and saves the
 * transfer. The projection deliberately mirrors sugar/metadata.py so that a client sees the
 * same shape whether or not this package is installed.
 *
 * ACLs are applied: fields the calling user cannot read are omitted, and fields they cannot
 * write are marked read-only. The rules are Sugar's own (SugarACL), so this cannot grant
 * access the user does not have.
 */

class McpSchemaApi extends SugarApi
{
    /**
     * Vardef keys worth returning. Everything else is display styling, duplicate-merge
     * configuration, full-text-search tuning or comments.
     */
    protected $keepKeys = [
        'type', 'dbType', 'len', 'required', 'default', 'sortable', 'pii',
        'relationship', 'module', 'bean_name', 'id_name', 'rname', 'link',
    ];

    public function registerApiRest()
    {
        return [
            'mcpSchema' => [
                'reqType' => 'GET',
                'path' => ['mcp', 'schema', '?'],
                'pathVars' => ['', '', 'module'],
                'method' => 'getMcpSchema',
                'shortHelp' => 'Pruned field schema for one module, for MCP clients',
                'longHelp' => '',
            ],
        ];
    }

    /**
     * @param ServiceBase $api
     * @param array $args  module (path), optional: fields (csv), include_links
     * @return array
     */
    public function getMcpSchema(ServiceBase $api, array $args)
    {
        $module = $args['module'];

        $bean = BeanFactory::newBean($module);
        if (empty($bean)) {
            throw new SugarApiExceptionNotFound("No such module: {$module}");
        }
        if (!SugarACL::checkAccess($module, 'access')) {
            throw new SugarApiExceptionNotAuthorized("No access to module: {$module}");
        }

        // Needed by projectField() so translate() resolves module-scoped label keys.
        $this->currentModule = $module;

        // Built with a loop rather than array_map(): Module Loader's package scanner denies
        // every callback-taking function, since a callback can smuggle in arbitrary code.
        $wanted = [];
        if (!empty($args['fields'])) {
            foreach (explode(',', $args['fields']) as $requested) {
                $requested = trim($requested);
                if ($requested !== '') {
                    $wanted[$requested] = true;
                }
            }
        }
        $includeLinks = !empty($args['include_links']);

        $fieldAcls = SugarACL::getUserAccess($module, [], ['bean' => $bean]);

        $fields = [];
        $links = [];
        $hidden = 0;

        foreach ($bean->field_defs as $name => $def) {
            if (!empty($wanted) && !isset($wanted[$name])) {
                continue;
            }

            // Field-level ACL. Sugar reports only denials, so an absent entry is full
            // access — reading it as an allowlist would invert every permission.
            $access = isset($fieldAcls[$name]) ? $fieldAcls[$name] : [];
            if (isset($access['read']) && $access['read'] === false) {
                $hidden++;
                continue;
            }

            $entry = $this->projectField($name, $def);

            $writable = !(isset($access['write']) && $access['write'] === false);
            if (!$writable) {
                $entry['readonly'] = true;
            }
            if (isset($access['license']) && $access['license'] === false) {
                $entry['readonly'] = true;
                $entry['readonly_reason'] = 'license';
            }

            if (isset($def['type']) && $def['type'] === 'link') {
                if ($includeLinks) {
                    $links[$name] = $entry;
                }
            } else {
                $fields[$name] = $entry;
            }
        }

        $result = [
            'module' => $module,
            'fields' => $fields,
            'field_count' => count($fields),
        ];
        if ($includeLinks) {
            $result['links'] = $links;
            $result['link_count'] = count($links);
        }
        if ($hidden > 0) {
            $result['hidden_field_count'] = $hidden;
        }

        return $result;
    }

    /**
     * Reduce one vardef to the projection, resolving its label.
     */
    protected function projectField($name, array $def)
    {
        $entry = [];

        foreach ($this->keepKeys as $key) {
            if (isset($def[$key]) && $def[$key] !== '' && $def[$key] !== false) {
                $entry[$key] = $def[$key];
            }
        }

        // Vardefs carry only a label *key* (vname). Resolving it here saves the client a
        // separate ~1.7 MB GET /lang/:lang, which has no per-module variant.
        if (!empty($def['vname'])) {
            $label = translate($def['vname'], $this->currentModule);
            if (is_string($label) && $label !== $def['vname']) {
                $entry['label'] = rtrim($label, ': ');
            }
        } elseif (!empty($def['labelValue'])) {
            $entry['label'] = $def['labelValue'];
        }

        if (!empty($def['readonly']) || !empty($def['readonly_formula'])) {
            $entry['readonly'] = true;
        }
        if (!empty($def['calculated'])) {
            $entry['calculated'] = true;
        }
        if (isset($def['source']) && $def['source'] === 'custom_fields') {
            $entry['custom'] = true;
        }

        // Only real value-bearing dropdowns. Non-enum fields often name a *search filter*
        // dropdown here (date_modified says date_range_search_dom), which is not a set of
        // values the field can hold and would mislead a writer.
        $type = isset($def['type']) ? $def['type'] : '';
        if (in_array($type, ['enum', 'multienum', 'radioenum', 'dynamicenum'], true)) {
            if (!empty($def['options']) && is_string($def['options'])) {
                $entry['options'] = $def['options'];
            }
        } else {
            unset($entry['options']);
        }

        return $entry;
    }

    /** Set while iterating so translate() can resolve module-scoped labels. */
    protected $currentModule = '';
}
