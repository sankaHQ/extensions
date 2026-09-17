import { useState } from "react";
import { StyleSheet, Switch, Text, View } from "react-native";

export default function SettingsScreen() {
  const [notifications, setNotifications] = useState(true);
  return (
    <View style={styles.container}>
      <View style={styles.row}>
        <Text>Notifications</Text>
        <Switch value={notifications} onValueChange={setNotifications} />
      </View>
      <Text>{notifications ? "On" : "Off"}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, padding: 16, gap: 8 },
  row: { flexDirection: "row", justifyContent: "space-between", alignItems: "center" },
});
