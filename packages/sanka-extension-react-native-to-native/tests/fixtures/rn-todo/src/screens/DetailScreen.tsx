import { useState } from "react";
import { Image, Pressable, StyleSheet, Switch, Text, View } from "react-native";
import type { NativeStackScreenProps } from "@react-navigation/native-stack";
import type { RootStackParamList } from "../../App";

type Props = NativeStackScreenProps<RootStackParamList, "Detail">;

export default function DetailScreen({ route, navigation }: Props) {
  const [done, setDone] = useState(false);
  return (
    <View style={styles.container}>
      <Image source={require("../../assets/logo.png")} style={styles.logo} resizeMode="contain" />
      <Text style={styles.title}>{route.params.title}</Text>
      <Text>Todo #{route.params.id}</Text>
      <View style={styles.rowBetween}>
        <Text>Done</Text>
        <Switch value={done} onValueChange={setDone} />
      </View>
      <Text>{done ? "Completed" : "Open"}</Text>
      <Pressable style={styles.button} onPress={() => navigation.goBack()}>
        <Text style={styles.buttonText}>Back</Text>
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, padding: 16, gap: 12 },
  logo: { width: 64, height: 64, alignSelf: "center" },
  title: { fontSize: 20, fontWeight: "bold" },
  rowBetween: { flexDirection: "row", justifyContent: "space-between", alignItems: "center" },
  button: { backgroundColor: "#0a84ff", padding: 12, borderRadius: 8, alignItems: "center" },
  buttonText: { color: "#ffffff" },
});
