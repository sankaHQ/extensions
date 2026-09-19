import { useEffect, useState } from "react";
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from "react-native";
import type { NativeStackScreenProps } from "@react-navigation/native-stack";
import type { RootStackParamList } from "../../App";
import type { Item } from "../models";

type Props = NativeStackScreenProps<RootStackParamList, "Detail">;
const API_BASE = "https://catalog.example.com";

export default function ItemDetailScreen({ route, navigation }: Props) {
  const [item, setItem] = useState<Item | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  const load = async () => {
    setLoading(true);
    setError(false);
    try {
      const response = await fetch(`${API_BASE}/api/items/${route.params.id}`);
      if (!response.ok) {
        throw new Error("request failed");
      }
      const data: Item = await response.json();
      setItem(data);
    } catch {
      setError(true);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  return (
    <View style={styles.container}>
      <Text>Item #{route.params.id}</Text>
      {loading ? <ActivityIndicator /> : null}
      {error ? <Text style={styles.error}>Could not load the item</Text> : null}
      {item ? <Text style={styles.title}>{item.title}</Text> : null}
      {item ? <Text>{item.done ? "Done" : "Open"}</Text> : null}
      <Pressable style={styles.button} onPress={() => navigation.goBack()}>
        <Text style={styles.buttonText}>Back</Text>
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, padding: 16, gap: 12 },
  title: { fontSize: 20, fontWeight: "bold" },
  error: { color: "#ff3b30" },
  button: { backgroundColor: "#0a84ff", padding: 12, borderRadius: 8, alignItems: "center" },
  buttonText: { color: "#ffffff" },
});
