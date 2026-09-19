import { useEffect, useState } from "react";
import { Pressable, StyleSheet, Text, TextInput, View } from "react-native";
import AsyncStorage from "@react-native-async-storage/async-storage";
import type { NativeStackScreenProps } from "@react-navigation/native-stack";
import type { RootStackParamList } from "../../App";

type Props = NativeStackScreenProps<RootStackParamList, "Create">;
const API_BASE = "https://catalog.example.com";

export default function CreateItemScreen({ navigation }: Props) {
  const [title, setTitle] = useState("");
  const [token, setToken] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    AsyncStorage.getItem("token").then((value) => setToken(value ?? ""));
  }, []);

  const submit = async () => {
    setSaving(true);
    try {
      const response = await fetch(`${API_BASE}/api/items`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify({ title, done: false }),
      });
      if (!response.ok) {
        throw new Error("request failed");
      }
      navigation.goBack();
    } catch {
      // The form keeps its values; the user can retry.
    } finally {
      setSaving(false);
    }
  };

  return (
    <View style={styles.container}>
      <TextInput style={styles.input} value={title} onChangeText={setTitle} placeholder="Title" />
      <Pressable style={styles.button} onPress={submit} disabled={saving}>
        <Text style={styles.buttonText}>Save</Text>
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, padding: 16, gap: 12 },
  input: { borderWidth: 1, borderColor: "#cccccc", borderRadius: 8, padding: 8 },
  button: { backgroundColor: "#0a84ff", padding: 12, borderRadius: 8, alignItems: "center" },
  buttonText: { color: "#ffffff" },
});
