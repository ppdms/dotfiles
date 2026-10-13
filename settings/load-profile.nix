{
  lib,
  profile,
  platform,
}:
let
  schema = builtins.fromJSON (builtins.readFile ./profile-schema.json);
  fail = path: message: throw "Invalid private profile at ${path}: ${message}";
  resolve =
    rule: if rule ? "$ref" then schema."$defs".${lib.removePrefix "#/$defs/" rule."$ref"} else rule;
  hasType =
    kind: value:
    if kind == "object" then
      builtins.isAttrs value
    else if kind == "array" then
      builtins.isList value
    else if kind == "string" then
      builtins.isString value
    else if kind == "integer" then
      builtins.isInt value
    else if kind == "boolean" then
      builtins.isBool value
    else if kind == "null" then
      value == null
    else
      false;
  sensitive =
    value:
    lib.any (marker: lib.hasInfix marker value) [
      "PRIVATE KEY"
      "AGE-SECRET-KEY-"
      "age-secret-key-"
    ];
  forbiddenName =
    name:
    builtins.match ".*([Aa][Pp][Ii][_-]?[Kk][Ee][Yy]|[Pp][Aa][Ss][Ss][Ww][Oo][Rr][Dd]|[Aa][Cc][Cc][Ee][Ss][Ss][_-]?[Tt][Oo][Kk][Ee][Nn]|[Pp][Rr][Ii][Vv][Aa][Tt][Ee][_-]?[Kk][Ee][Yy]|[Aa][Uu][Tt][Hh][_-]?[Dd][Aa][Tt][Aa]).*" name
    != null;
  validate =
    rawRule: value: path:
    let
      rule = resolve rawRule;
      types = if builtins.isList (rule.type or null) then rule.type else [ (rule.type or null) ];
      properties = rule.properties or { };
      additional = rule.additionalProperties or false;
      unknown =
        if builtins.isAttrs value then
          lib.subtractLists (builtins.attrNames properties) (builtins.attrNames value)
        else
          [ ];
      supplied = if builtins.isAttrs value then builtins.mapAttrs (_: child: child) value else { };
      defaults =
        lib.mapAttrs
          (
            _: rawChild:
            let
              child = resolve rawChild;
            in
            if child ? default then
              child.default
            else if child ? "x-default-from" && builtins.hasAttr child."x-default-from" supplied then
              supplied.${child."x-default-from"}
            else
              null
          )
          (
            lib.filterAttrs (
              name: rawChild:
              let
                child = resolve rawChild;
              in
              !(builtins.hasAttr name supplied) && (child ? default || child ? "x-default-from")
            ) properties
          );
      merged = defaults // supplied;
      missing = lib.filter (name: !(builtins.hasAttr name merged)) (rule.required or [ ]);
      branches = rule.anyOf or [ ];
      branchMatches = branch: lib.all (name: builtins.hasAttr name merged) (branch.required or [ ]);
      objectValue = builtins.mapAttrs (
        name: child:
        let
          childRule = properties.${name} or additional;
        in
        if builtins.hasAttr name properties then
          validate childRule child "${path}.${name}"
        else
          validate childRule child "${path}.<entry>"
      ) merged;
      keyChecks = lib.all (
        name:
        if forbiddenName name then
          fail path "credential-bearing field names are forbidden"
        else if rule ? propertyNames then
          builtins.deepSeq (validate rule.propertyNames name "${path}.<name>") true
        else
          true
      ) (builtins.attrNames merged);
    in
    if !(lib.any (kind: hasType kind value) types) then
      fail path "unexpected value type"
    else if rule ? enum && !(lib.elem value rule.enum) then
      fail path "value is not an approved choice"
    else if value == null then
      null
    else if builtins.isAttrs value then
      if unknown != [ ] && additional == false then
        fail path "unrecognized fields"
      else if missing != [ ] then
        fail path "missing required fields"
      else if builtins.length (builtins.attrNames value) < (rule.minProperties or 0) then
        fail path "too few entries"
      else if branches != [ ] && !(lib.any branchMatches branches) then
        fail path "missing required field combination"
      else
        assert keyChecks;
        objectValue
    else if builtins.isList value then
      if builtins.length value > (rule.maxItems or 256) then
        fail path "too many entries"
      else
        lib.imap0 (index: child: validate rule.items child "${path}[${toString index}]") value
    else if builtins.isString value then
      if sensitive value then
        fail path "private-key material is forbidden"
      else if
        lib.hasInfix "\n" value || lib.hasInfix "\r" value || builtins.match ".*[[:cntrl:]].*" value != null
      then
        fail path "control characters are forbidden"
      else if builtins.match ".*://[^/]*:[^/@]*@.*" value != null then
        fail path "inline URI credentials are forbidden"
      else if
        builtins.stringLength value < (rule.minLength or 0)
        || builtins.stringLength value > (rule.maxLength or 8192)
      then
        fail path "invalid string length"
      else if
        rule ? pattern
        && builtins.match (lib.removeSuffix "$" (lib.removePrefix "^" rule.pattern)) value == null
      then
        fail path "invalid string format"
      else
        value
    else if builtins.isInt value then
      if rule ? minimum && value < rule.minimum || rule ? maximum && value > rule.maximum then
        fail path "integer is outside the supported range"
      else
        value
    else
      value;
  selected =
    if
      !(lib.elem platform [
        "darwin"
        "nixos"
      ])
    then
      fail "platform" "unsupported platform"
    else if !(profile ? profiles) || !(builtins.hasAttr platform profile.profiles) then
      fail "profiles" "requested platform is missing"
    else
      validate schema."$defs".${platform} profile.profiles.${platform} "profiles.${platform}";
in
builtins.deepSeq selected selected
