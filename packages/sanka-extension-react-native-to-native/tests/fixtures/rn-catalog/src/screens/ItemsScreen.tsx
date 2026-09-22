import { useEffect, useState } from "react";
import { ActivityIndicator, FlatList, Pressable, StyleSheet, Text, View } from "react-native";
import type { NativeStackScreenProps } from "@react-navigation/native-stack";
import type { RootStackParamList } from "../../App";
import type { Item } from "../models";

type Props = NativeStackScreenProps<RootStackParamList, "Items">;
const API_BASE = "https://catalog.example.com";

export default function ItemsScreen({ navigation }: Props) {
  const [items, setItems] = useState<Item[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  const load = async () => {
    setLoading(true);
    setError(false);
    try {
      const response = await fetch(`${API_BASE}/api/items`);
      if (!response.ok) {
        throw new Error("request failed");
      }
      const data: Item[] = await response.json();
      setItems(data);
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
      <View style={styles.actions}>
        <Pressable style={styles.button} onPress={() => navigation.navigate("Create")}>
          <Text style={styles.buttonText}>New item</Text>
        </Pressable>
        <Pressable style={styles.button} onPress={() => navigation.navigate("Settings")}>
          <Text style={styles.buttonText}>Settings</Text>
        </Pressable>
      </View>
      {loading ? <ActivityIndicator /> : null}
      {error ? <Text style={styles.error}>Could not load items</Text> : null}
      <FlatList
        data={items}
        keyExtractor={(item) => String(item.id)}
        refreshing={loading}
        onRefresh={load}
        renderItem={({ item }) => (
          <Pressable style={styles.row} onPress={() => navigation.navigate("Detail", { id: item.id })}>
            <Text style={styles.rowText}>{item.title}</Text>
            {item.done ? <Text>done</Text> : null}
          </Pressable>
        )}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, padding: 16 },
  actions: { flexDirection: "row", gap: 8, marginBottom: 12 },
  button: { backgroundColor: "#0a84ff", padding: 12, borderRadius: 8 },
  buttonText: { color: "#ffffff", fontWeight: "600" },
  error: { color: "#ff3b30" },
  row: { paddingVertical: 12, borderBottomWidth: 1, borderColor: "#eeeeee" },
  rowText: { fontSize: 16 },
});
