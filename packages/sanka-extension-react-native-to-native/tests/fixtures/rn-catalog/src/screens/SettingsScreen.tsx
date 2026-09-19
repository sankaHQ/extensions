import { useEffect, useState } from "react";
import { Pressable, StyleSheet, Text, TextInput, View } from "react-native";
import AsyncStorage from "@react-native-async-storage/async-storage";

export default function SettingsScreen() {
  const [token, setToken] = useState("");
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    AsyncStorage.getItem("token").then((value) => setToken(value ?? ""));
  }, []);

  return (
    <View style={styles.container}>
      <Text>API token</Text>
      <TextInput style={styles.input} value={token} onChangeText={setToken} secureTextEntry />
      <Pressable
        style={styles.button}
        onPress={() => {
          AsyncStorage.setItem("token", token);
          setSaved(true);
        }}
      >
        <Text style={styles.buttonText}>Save token</Text>
      </Pressable>
      {saved ? <Text>Saved</Text> : null}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, padding: 16, gap: 12 },
  input: { borderWidth: 1, borderColor: "#cccccc", borderRadius: 8, padding: 8 },
  button: { backgroundColor: "#0a84ff", padding: 12, borderRadius: 8, alignItems: "center" },
  buttonText: { color: "#ffffff" },
});
