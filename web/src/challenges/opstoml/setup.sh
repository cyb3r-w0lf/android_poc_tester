set -e
PKG=com.pentathon.opsscheduler
SP=/data/data/$PKG/shared_prefs
mkdir -p "$SP"
cat > "$SP/pp.xml" <<PPEOF
<?xml version='1.0' encoding='utf-8' standalone='yes' ?>
<map>
    <string name="url">http://10.0.2.2:3014</string>
</map>
PPEOF
UID_=$(stat -c %u /data/data/$PKG)
chown "$UID_:$UID_" "$SP" "$SP/pp.xml"
restorecon -R "$SP" 2>/dev/null || true
# prove the dynamic flag (CHALLENGE_FLAG_OPSTOML) reached this script as $FLAG:
echo "$FLAG" > /data/local/tmp/opstoml_flag.txt
